import os
import re

from nifiapi.__jvm__ import JvmHolder


class Attributes:
    """What evaluateAttributeExpressions reads when there is no FlowFile: these values, then the environment.

    NiFi 2.11 answers a plain ${NAME} from this object's getAttribute alone and never asks Java, whose own lookup
    falls back to environment variables, so the fallback has to be here for ENVIRONMENT scoped properties.
    """
    def __init__(self, values=None):
        self.values = values or {}

    # The names mirror NiFi's Java FlowFile API.
    def getAttribute(self, name):  # NOSONAR(S100)
        value = self.values.get(name)
        return os.environ.get(name) if value is None else value

    # The names mirror NiFi's Java FlowFile API.
    def getAttributes(self):  # NOSONAR(S100)
        java = JvmHolder.jvm.java.util.HashMap()
        for key, value in self.values.items():
            java.put(key, value)
        return java


# plain attribute names, as nifiapi.properties.PropertyContext's fast path for ${NAME} reads them, less the
# parentheses of a function such as hostname()
_NAME = r"[^${}()\[\],:;/*' \t\r\n\\d][^${}()\[\],:;/*' \t\r\n]*"
_REFERENCE = re.compile(r"\$\{(" + _NAME + r")}")
_PLAIN = re.compile(r"(?:[^$]|\$\{" + _NAME + r"})*")


def evaluate(property_value, attributes):
    """The string value of property_value with its expression language resolved against attributes.

    NiFi 2.11 reads a value that merely starts with ${NAME} as that bare reference and drops whatever follows, so
    ${DATA_DIR}/exports becomes the value of DATA_DIR, and ${BASE}/${TAG:toLower()} becomes the value of BASE. A
    value made only of text and plain ${NAME} references is therefore resolved here, whole. Anything else is
    evaluated whole by the Java PropertyValue behind property_value, which skips that shortcut.
    """
    raw = property_value.getValue()
    if raw and getattr(property_value, "el_supported", False) and property_value.isExpressionLanguagePresent():
        if _PLAIN.fullmatch(raw):
            return _REFERENCE.sub(lambda match: attributes.getAttribute(match.group(1)) or "", raw)
        java = getattr(property_value, "property_value", None)
        if java is not None:
            return java.evaluateAttributeExpressions(attributes.getAttributes()).getValue()
        return property_value.evaluateAttributeExpressions(attributes).getValue()
    return raw
