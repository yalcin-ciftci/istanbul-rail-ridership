select
    dataset,
    resource_id,
    source_month,
    source_last_modified,
    source_sha256,
    stored_sha256,
    stored_path,
    row_count,
    ingested_at,
    hash_note
from {{ source('raw', 'source_versions') }}
