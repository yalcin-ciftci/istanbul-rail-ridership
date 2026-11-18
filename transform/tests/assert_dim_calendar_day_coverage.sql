select count(*) as row_count
from {{ ref('dim_calendar_day') }}
-- dbt --empty replaces ref inputs with zero-row relations, including test inputs.
having count(*) != {% if flags.EMPTY %}0{% else %}date_diff(
    'day',
    date '{{ var("calendar_start_date", "2021-01-01") }}',
    date '{{ var("calendar_end_date", "2025-12-31") }}'
) + 1{% endif %}
