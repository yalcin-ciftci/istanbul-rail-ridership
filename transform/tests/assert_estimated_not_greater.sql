{% for model in ['int_rail_entrance_daily', 'fct_station_daily', 'fct_line_daily'] %}
select
    '{{ model }}' as model_name,
    ride_date,
    journey_count,
    journey_count_estimated
from {{ ref(model) }}
where journey_count_estimated > journey_count
{% if not loop.last %}union all{% endif %}
{% endfor %}
