"""Reference aspect metadata is not by itself a reference-following requirement."""


def is_reference_requirement(item):
    # Numeric/user-specified requirements can carry ref_aspect=typography/etc.
    # Their ground truth is the request, not the reference page; e.g. font>=18px.
    return bool(item.get("ref_aspect")) and not (
        item.get("seed_kind") == "spec" or item.get("axis") == "specify"
        or item.get("ref_directive") in {"specify", "ignore"}
        or item.get("answer_source") in {"题面", "user_request", "request", "prompt"})
