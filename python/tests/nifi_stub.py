"""A stand-in for the NiFi 2.11 Python API (nifiapi), so the processors import and run in a unit test.

Where the real code matters it is copied in behaviour: PythonPropertyValue.evaluateAttributeExpressions takes its
fast path for a plain ${NAME} and never asks Java, and the state manager refuses a value over 65535 bytes the way
ZooKeeperStateProvider's DataOutputStream.writeUTF does.
"""
import enum
import os
import re
import sys
import types

HERE = os.path.join(os.path.dirname(__file__), "..", "container_registry")
TRIVIAL = re.compile(r"\$\{([^${}\[\],:;/*\' \t\r\n\\d][^${}\[\],:;/*\' \t\r\n]*)}")
WRITE_UTF_LIMIT = 65535


class JavaMap(dict):
    def put(self, key, value):
        self[key] = value


class _Namespace:
    def __getattr__(self, name):
        return _Namespace() if name != "HashMap" else JavaMap


class StateError(Exception):
    pass


class FakeStateMap:
    """nifiapi.componentstate.StateMap in NiFi 2.11: getStateVersion, get and toMap, and nothing else, so no
    `in`, no iteration and no items()."""
    def __init__(self, values):
        self._values = dict(values)

    def getStateVersion(self):
        return None

    def get(self, key):
        return self._values.get(key)

    def toMap(self):
        return dict(self._values)


class FakeStateManager:
    """Cluster state kept across runs, with ZooKeeperStateProvider's per-value limits."""
    def __init__(self):
        self.values = {}
        self.fail_next = None

    def getState(self, scope):
        return FakeStateMap(self.values)

    def setState(self, state, scope):
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            raise error
        for key, value in state.items():
            if len(key.encode()) > WRITE_UTF_LIMIT or len(value.encode()) > WRITE_UTF_LIMIT:
                raise StateError(f"UTFDataFormatException: encoded string too long for {key}")
        if sum(len(k) + len(v) for k, v in state.items()) > 1024 * 1024:
            raise StateError("state over 1 MB")
        self.values = dict(state)


class JavaValue:
    """The Java PropertyValue behind a PythonPropertyValue: it evaluates a whole expression, a few functions
    included, and its attribute lookup falls back to the environment."""
    def __init__(self, string, environment):
        self.string = string
        self.environment = environment

    def evaluateAttributeExpressions(self, attributes):
        def one(match):
            name, _, function = match.group(1).partition(":")
            if name == "hostname()":
                return "nifi-node-a"
            text = attributes.get(name, self.environment.get(name, ""))
            return text.lower() if function == "toLower()" else text
        return Value(re.sub(r"\$\{([^}]+)}", one, self.string), True)._plain()


class Value:
    """PythonPropertyValue: a value that starts with a plain ${NAME} is read as that reference alone, the way
    PropertyContext.create_python_property_value matches it, and anything else is evaluated by Java."""
    def __init__(self, string, el_supported, environment=None):
        self.string = string
        self.el_supported = el_supported
        self.environment = environment if environment is not None else os.environ
        self.el_present = string is not None and "${" in string
        match = TRIVIAL.match(string) if self.el_present else None
        self.referenced = match.group(1) if match else None
        self.property_value = JavaValue(string, self.environment)

    def getValue(self):
        return self.string

    def isExpressionLanguagePresent(self):
        return self.el_present

    def evaluateAttributeExpressions(self, attribute_map=None):
        if not (self.el_supported and self.el_present):
            return self
        if self.referenced is not None:
            found = attribute_map.getAttribute(self.referenced)
            return Value(found if found is not None else "", self.el_supported)._plain()
        return self.property_value.evaluateAttributeExpressions(attribute_map.getAttributes())

    def _plain(self):
        self.el_present = False
        return self


class FakeContext:
    """ProcessContext: values maps a property name to its configured string."""
    def __init__(self, descriptors, values, dynamic=None, state=None):
        self.descriptors = {d.name: d for d in descriptors}
        self.values = dict(values)
        self.dynamic = dict(dynamic or {})
        self.state = state or FakeStateManager()
        self.services = {}   # property name -> the object asControllerService returns

    def getProperty(self, descriptor):
        name = descriptor.name
        if name in self.values:
            string = self.values[name]
        elif name in self.dynamic:
            string = self.dynamic[name]
        else:
            string = getattr(descriptor, "default_value", None)
        value = Value(string, getattr(descriptor, "expression_language_scope", None) is not None
                      and descriptor.expression_language_scope != ExpressionLanguageScope.NONE)
        value.asControllerService = lambda: self.services[name]
        return value

    def getProperties(self):
        return [types.SimpleNamespace(name=n, dynamic=True,
                                      expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
                for n in self.dynamic] + list(self.descriptors.values())

    def getStateManager(self):
        return self.state


class ExpressionLanguageScope(enum.Enum):
    NONE = 1
    ENVIRONMENT = 2
    FLOWFILE_ATTRIBUTES = 3


class PropertyDescriptor:
    def __init__(self, name, description, required=False, sensitive=False, default_value=None,
                 allowable_values=None, validators=None, expression_language_scope=ExpressionLanguageScope.NONE,
                 dependencies=None, dynamic=False, controller_service_definition=None):
        self.name, self.required, self.sensitive = name, required, sensitive
        self.description, self.allowable_values = description, allowable_values
        self.default_value, self.dynamic = default_value, dynamic
        self.expression_language_scope = expression_language_scope


class PropertyDependency:
    def __init__(self, descriptor, *values):
        self.descriptor, self.values = descriptor, values


class FlowFileSource:
    logger = None

    def __init__(self):
        pass


class FlowFileSourceResult:
    def __init__(self, relationship, attributes=None, contents=None):
        self.relationship, self.attributes, self.contents = relationship, attributes, contents


class FlowFileTransform:
    logger = None

    def __init__(self):
        pass


class FlowFileTransformResult:
    def __init__(self, relationship, attributes=None, contents=None):
        self.relationship, self.attributes, self.contents = relationship, attributes, contents


class Logger:
    def __init__(self):
        self.lines = []

    def _log(self, level, message):
        self.lines.append((level, str(message)))

    def info(self, message):
        self._log("info", message)

    def warn(self, message):
        self._log("warn", message)

    def error(self, message):
        self._log("error", message)

    def text(self):
        return "\n".join(m for _l, m in self.lines)


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def install():
    if "nifiapi.stub" in sys.modules:
        return
    _module("nifiapi", __path__=[])
    _module("nifiapi.stub")
    _module("nifiapi.__jvm__", JvmHolder=types.SimpleNamespace(jvm=_Namespace()))
    _module("nifiapi.componentstate", Scope=types.SimpleNamespace(CLUSTER="CLUSTER", LOCAL="LOCAL"))
    _module("nifiapi.documentation", use_case=lambda **k: (lambda cls: cls))
    _module("nifiapi.flowfilesource", FlowFileSource=FlowFileSource, FlowFileSourceResult=FlowFileSourceResult)
    _module("nifiapi.flowfiletransform", FlowFileTransform=FlowFileTransform,
            FlowFileTransformResult=FlowFileTransformResult)
    _module("nifiapi.properties", ExpressionLanguageScope=ExpressionLanguageScope, PropertyDependency=PropertyDependency,
            PropertyDescriptor=PropertyDescriptor,
            StandardValidators=types.SimpleNamespace(URL_VALIDATOR=0, POSITIVE_INTEGER_VALIDATOR=0,
                                                     REGULAR_EXPRESSION_VALIDATOR=0, DATA_SIZE_VALIDATOR=0))
    _module("nifiapi.relationship", Relationship=lambda *a, **k: a[0])
    if HERE not in sys.path:
        sys.path.insert(0, HERE)


install()
