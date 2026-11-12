from ..subjects import comparison_result


def resolve(journal, subject_ref, *, kind="comparison_result"):
    if kind == "assembly":
        from ..assembly.subject import resolve as assembly_subject

        return assembly_subject(journal, subject_ref)
    return comparison_result(journal, subject_ref)
