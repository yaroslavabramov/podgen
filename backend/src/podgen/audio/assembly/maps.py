from podgen.domain.errors import ContractError


def final_master_map(parts, source_rate, master_rate, master_frames):
    if abs(master_frames - round(parts[-1]["master_end_frame"] * master_rate / source_rate)) > 1:
        raise ContractError("assembly_resampling_changed_duration")
    result = []
    previous = 0
    for i, row in enumerate(parts):
        end = (
            master_frames
            if i == len(parts) - 1
            else round(row["master_end_frame"] * master_rate / source_rate)
        )
        if end <= previous:
            raise ContractError("assembly_invalid_master_frame_map")
        result.append(
            dict(
                row,
                assembly_start_frame=row["master_start_frame"],
                assembly_end_frame=row["master_end_frame"],
                master_start_frame=previous,
                master_end_frame=end,
                source_sample_rate_hz=source_rate,
                master_sample_rate_hz=master_rate,
                boundary_conversion="nearest_frame_with_measured_final_endpoint",
            )
        )
        previous = end
    return result
