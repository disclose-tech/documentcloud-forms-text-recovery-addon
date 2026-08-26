from pypdf.generic import IndirectObject


def follow_ref(obj):
    """Follow an indirect reference to the object it points at.

    Anything that is not a reference is already the object, and comes back
    unchanged, so a caller never has to know which of the two it holds.
    """
    return obj.get_object() if isinstance(obj, IndirectObject) else obj
