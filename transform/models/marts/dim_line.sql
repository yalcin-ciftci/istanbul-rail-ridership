select
    line_name,
    regexp_extract(line_name, '^[^- ]+', 0) as line_code,
    bool_or(is_line_level) as has_line_level_rows,
    min(ride_date) as first_seen_date,
    max(ride_date) as last_seen_date
from {{ ref('int_rail_entrance_daily') }}
group by line_name
