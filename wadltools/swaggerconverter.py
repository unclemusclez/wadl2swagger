import os
import re
import json
import yaml
import textwrap
import logging
from collections import OrderedDict
import wadllib
from wadltools.wadl import WADL, DocHelper, BadWADLError
from wadllib.application import Parameter, WADLError, UnsupportedMediaTypeError


class WADLParseError(Exception):

    def __init__(self, orig_message, wadl_file, location, cause):
        message = "%s in %s ('%s'), caused by %s" % (
            orig_message,
            wadl_file,
            location,
            repr(cause),
        )
        super(WADLParseError, self).__init__(message)
        self.wadl_file = wadl_file
        self.location = location
        self.cause = cause


def merge_dicts(a, b, path=None):
    if path is None:
        path = []
    for key in b:
        if key in a:
            if isinstance(a[key], dict) and isinstance(b[key], dict):
                merge_dicts(a[key], b[key], path + [str(key)])
            elif a[key] == b[key]:
                pass  # same leaf value
            else:
                raise Exception("Conflict at %s" % ".".join(path + [str(key)]))
        else:
            a[key] = b[key]
    return a


class SwaggerConverter:

    def __init__(self, options):
        self.options = options
        self.autofix = options.autofix
        self.strict = options.strict
        self.merge_dir = options.merge_dir
        self.wadl_file = None

    def _process_resource_parameters(self, resource, path, swagger_resource):
        """Process parameters for a resource to reduce nesting."""
        try:
            # wadllib can't get parameters w/out media types (e.g. path params?)
            try:
                params = resource.parameters("application/json")
            except (UnsupportedMediaTypeError, AttributeError):
                self.logger.warning(
                    "No support for application/json for resource at %s", path
                )
                params = []
            for param in resource.tag.findall("./" + WADL.qname("wadl", "param")):
                params.append(Parameter(resource, param))
            for param in params:
                swagger_param = self.build_param(param)
                if swagger_param is not None:
                    if "parameters" not in swagger_resource:
                        swagger_resource["parameters"] = []
                    swagger_resource["parameters"].append(swagger_param)
        except AttributeError:
            self.logger.debug(
                "   WARN: wadllib can't get parameters, possibly a wadllib bug"
            )
            self.logger.debug(
                "     (It seems like it only works if the resource has a GET method"
            )

    def _process_method(self, method, resource, path, swagger_resource):
        """Process a method to reduce nesting."""
        self.logger.debug("    Processing method %s %s", method.name, path)
        verb = method.name
        if self.autofix and verb == "copy":
            self.logger.warning(
                "Autofix: Using PUT instead of COPY verb (OpenStack services accept either, Swagger does not allow COPY)"
            )
            verb = "put"
        swagger_method = swagger_resource[verb] = OrderedDict()
        # Rackspace specific...
        if "{http://docs.rackspace.com/api}id" in method.tag.attrib:
            swagger_method["operationId"] = method.tag.attrib[
                "{http://docs.rackspace.com/api}id"
            ]
        swagger_method["summary"] = self.build_summary(method, path)
        description = DocHelper.short_desc_as_markdown(method)
        if description is not None:
            swagger_method["description"] = folded(description)
        if "id" in method.tag.attrib:
            swagger_method["operationId"] = method.tag.attrib["id"]
        # swagger_method['consumes'] = []
        swagger_method["produces"] = []
        swagger_method["responses"] = OrderedDict()

        # Handle request
        if method.request.tag is not None:
            request = method.request
            for representation in request.representations:
                # Swagger schema needs to be updated to allow consumes here
                # swagger_method['consumes'].append(representation.media_type)
                for param in representation.params(resource):
                    swagger_param = self.build_param(param)
                    if swagger_param is not None:
                        if "parameters" not in swagger_method:
                            swagger_method["parameters"] = []
                        swagger_method["parameters"].append(swagger_param)
            # Add params from direct <param> tags in request
            for param in request.tag.findall("./" + WADL.qname("wadl", "param")):
                param_obj = Parameter(request, param)
                swagger_param = self.build_param(param_obj)
                if swagger_param is not None:
                    if "parameters" not in swagger_method:
                        swagger_method["parameters"] = []
                    swagger_method["parameters"].append(swagger_param)

        # Handle response
        self._process_response(method, swagger_method)

    def _process_response(self, method, swagger_method):
        """Process response and handle status autofix."""
        if method.response.tag is not None:
            response = method.response
            # Not properly iterable - plus we're focused on json
            # for representation in response.representation:
            representation = response.get_representation_definition("application/json")
            if representation is not None:
                swagger_method["produces"].append(representation.media_type)

            # Handle status
            try:
                statuses = response.tag.attrib["status"].split()
            except KeyError as e:
                if self.autofix:
                    self.logger.warning(
                        "Autofix: No status in response, setting default to '200'"
                    )
                    statuses = ["200"]
                else:
                    raise BadWADLError(
                        "Response has no status", e, self.wadl_file
                    ) from e

            for status in statuses:
                swagger_method["responses"][status] = self.build_response(
                    response, status
                )

                code_sample = None
                code_samples = response.tag.findall(
                    ".//"
                    + WADL.qname("docbook", "programlisting")
                    + '[@language="javascript"]'
                )
                if code_samples:
                    try:
                        # if there is more than one, the first is usually HTTP headers
                        code_sample = code_samples[-1].text
                        code_sample = self.fix_json(code_sample)
                    except ValueError as e:
                        error = WADLParseError(
                            "Unparsable code sample",
                            self.wadl_file,
                            swagger_method["summary"],
                            e,
                        )
                        if self.strict:
                            raise error
                        else:
                            self.logger.error(str(error))

                if code_sample:
                    swagger_method["responses"][status]["examples"] = (
                        self.build_code_sample(code_sample)
                    )

    def convert(self, title, wadl_file, swagger_file):
        try:
            self.logger = logging.getLogger(wadl_file)
            self.logger.info("Converting: %s to %s", wadl_file, swagger_file)

            defaults = self.default_swagger_dict(swagger_file)

            wadl = WADL.application_for(wadl_file)
            if self.autofix and wadl.resource_base is None:
                self.logger.warning(
                    "Autofix: No base path, setting to http://localhost"
                )
                wadl.resource_base = "http://localhost"
            self.logger.debug("Reading WADL from %s", wadl_file)
            swagger = OrderedDict()
            swagger["swagger"] = "2.0"
            swagger["info"] = OrderedDict()
            try:
                swagger["info"] = defaults["info"]
            except KeyError:
                swagger["info"]["title"] = title
                swagger["info"]["version"] = "Unknown"
            try:
                swagger["consumes"] = defaults["consumes"]
                swagger["produces"] = defaults["produces"]
            except KeyError:
                swagger["consumes"] = ["application/json"]
                swagger["produces"] = ["application/json"]

            swagger["paths"] = OrderedDict()

            for resource_element in wadl.resources or []:
                path = resource_element.attrib["path"]
                resource = wadl.get_resource_by_path(path)
                if resource is None:
                    self.logger.warning("Resource not found for path: %s", path)
                    continue
                if self.autofix and not path.startswith("/"):
                    self.logger.warning("Autofix: Adding leading / to path")
                    path = "/" + path
                swagger_resource = swagger["paths"][path] = OrderedDict()
                self.logger.debug("  Processing resource for %s", path)
                # Process resource parameters
                self._process_resource_parameters(resource, path, swagger_resource)

                for method in resource.method_iter:
                    self._process_method(method, resource, path, swagger_resource)
            swagger = merge_dicts(swagger, defaults)
            return swagger
        except WADLError as e:
            # except Exception as e:
            raise BadWADLError("Could not convert WADL", e, wadl_file)

    def fix_json(self, json_sample):
        try:
            json.loads(json_sample)
        except ValueError:
            if self.autofix:
                # sometimes the headers are in the same sample
                # strip them and check again
                match = re.match(
                    r".*^([\{\[].*)\Z", json_sample, (re.MULTILINE | re.DOTALL)
                )
                if match:
                    json_sample = match.group(1)
                    json.loads(json_sample)
            else:
                raise
        return json_sample

    def default_swagger_dict(self, swagger_file):
        filename, _ = os.path.splitext(os.path.split(swagger_file)[1])
        merge_file = os.path.join(self.merge_dir, filename + ".yaml")
        if os.path.isfile(merge_file):
            self.logger.info("Using defaults from %s" % merge_file)
            with open(merge_file, "r") as stream:
                swagger = OrderedDict(yaml.load(stream, Loader=yaml.SafeLoader))
        else:
            swagger = OrderedDict()
        return swagger

    def xsd_to_json_type(self, full_type):
        if full_type is None:
            return None

        if ":" in full_type:
            namespace, xsd_type = full_type.split(":")
            if namespace in ["", "xs"]:
                namespace = "xsd"
        else:
            namespace = "xsd"
            xsd_type = full_type

        typemap = {
            "xsd": {
                # array?
                "boolean": "boolean",  # is xsd:bool also valid?
                "int": "integer",
                "integer": "integer",
                "decimal": "number",
                # null?
                # object/complex types?
                "string": "string",
                "anyURI": {"type": "string", "format": "uri"},
                "dateTime": {"type": "string", "format": "date-time"},
                "date": "string",  # should be string w/ format or regex
                "time": "string",  # should be string w/ format or regex
            },
            "csapi": {
                "UUID": {
                    "type": "string"
                    # format/pattern?
                }
            },
        }
        # This should probably be more namespace aware (e.g. handle xs:string
        # or xsd:string)
        try:
            return typemap[namespace][xsd_type]
        except KeyError:
            return None

    def style_to_in(self, style):
        return {
            "matrix": "unknown",
            "query": "query",
            "header": "header",
            "template": "path",
            "plain": "body",
        }[style]

    def build_summary(self, documented_wadl_object, path):
        doc_tag = DocHelper.doc_tag(documented_wadl_object)
        if doc_tag is not None and "title" in doc_tag.attrib:
            return doc_tag.attrib["title"]
        return f"{documented_wadl_object.name.upper()} {path}"

    def build_param(self, wadl_param):
        self.logger.debug("Found param: %s" % wadl_param.name)

        param = OrderedDict()
        param["name"] = wadl_param.name
        param["required"] = wadl_param.is_required
        param["in"] = self.style_to_in(wadl_param.style)

        wadl_type = wadl_param.tag.get("type", "string")
        json_type = self.xsd_to_json_type(wadl_type)
        if json_type is None:
            self.logger.warning(
                "Unknown type: %s for param %s", wadl_type, wadl_param.name
            )
            if self.autofix:
                self.logger.warning("Using string for %s", wadl_type)
                json_type = "string"
            else:
                json_type = wadl_type
        if isinstance(json_type, dict):
            param.update(json_type)
        else:
            param["type"] = json_type

        if self.autofix:
            if param["in"] == "body":
                self.logger.warning(
                    "Ignoring body parameter, converting these is not yet supported..."
                )
                return None
            if param["in"] == "path":
                if param["required"] is not True:
                    self.logger.warning(
                        "Autofix: path parameters must be required in Swagger (%s)",
                        param["name"],
                    )
                    param["required"] = True

        if self.options.nodoc is not True:
            if (
                DocHelper.doc_tag(wadl_param) is not None
                and DocHelper.doc_tag(wadl_param).text is not None
            ):
                description = DocHelper.docbook_to_markdown(
                    DocHelper.doc_tag(wadl_param)
                )
                if description is not None:
                    # Cleanup whitespace...
                    description = textwrap.dedent(description)
                    param["description"] = folded(description)
        return param

    def format_id(self, id):
        return re.sub(r"([a-z])([A-Z])", r"\1 \2", id)

    def build_response(self, wadl_response, status):
        # status is passed to avoid KeyError and allow autofix
        doc_tag = DocHelper.doc_tag(wadl_response)
        if doc_tag is not None and doc_tag.text is not None:
            description = " ".join(doc_tag.text.split())
        else:
            description = "%s response" % status

        return {"description": folded(description)}

    def build_code_sample(self, wadl_code_sample):
        examples = OrderedDict()
        try:
            data = json.loads(wadl_code_sample)
            pretty = json.dumps(data, indent=4, separators=(",", ": "))
            examples["application/json"] = folded(pretty)
        except:
            pass
        return examples


# pyyaml presenters


class quoted(str):
    pass


def quoted_presenter(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')


yaml.add_representer(quoted, quoted_presenter)


class folded(str):
    pass


def folded_presenter(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=">")


yaml.add_representer(folded, folded_presenter)


class literal(str):
    pass


def literal_presenter(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.add_representer(literal, literal_presenter)


def ordered_dict_presenter(dumper, data):
    return dumper.represent_dict(data.items())


yaml.add_representer(OrderedDict, ordered_dict_presenter)
