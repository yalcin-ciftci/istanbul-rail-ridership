select m.line_code, m.daily_line_name
from {{ ref('external_line_map') }} as m
where not exists (
    select 1 from {{ ref('fct_line_daily') }} as d
    where d.line_name = m.daily_line_name
)
{% if flags.EMPTY %}
    and false
{% endif %}
