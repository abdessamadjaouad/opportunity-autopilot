from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject


def reject_active_content(reader):
    """Inspect indirect/compressed PDF objects without executing actions or changing the upload."""
    seen = set()
    count = 0
    forbidden = {"/JS", "/JavaScript", "/AA", "/OpenAction", "/Launch", "/EmbeddedFiles", "/RichMedia"}
    actions = {"/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/GoToR"}

    def visit(obj):
        nonlocal count
        if isinstance(obj, IndirectObject):
            key = (obj.idnum, obj.generation)
            if key in seen:
                return
            seen.add(key)
            obj = obj.get_object()
        count += 1
        if count > 100000:
            raise ValueError("PDF object limit exceeded")
        if isinstance(obj, DictionaryObject):
            if forbidden.intersection(obj.keys()) or str(obj.get("/S", "")) in actions:
                raise ValueError("Active PDF content is not allowed")
            for value in obj.values():
                visit(value)
        elif isinstance(obj, ArrayObject):
            for value in obj:
                visit(value)
    visit(reader.trailer)
