"""Read historical optional fields losslessly without manufacturing them in new records."""


def omit_unsupplied(instance, handler, *fields):
    data = handler(instance)
    for field in fields:
        if field not in instance.model_fields_set:
            data.pop(field, None)
    return data
