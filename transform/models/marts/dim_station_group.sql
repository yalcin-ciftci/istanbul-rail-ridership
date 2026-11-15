with group_attributes as (
    select
        station_group,
        string_agg(station_code, ', ' order by station_code) as station_codes,
        cast(count(*) as bigint) as station_code_count,
        string_agg(distinct district, ', ' order by district)
            filter (where district is not null) as districts,
        min(first_seen_date) as first_seen_date,
        max(last_seen_date) as last_seen_date
    from {{ ref('dim_station') }}
    where station_group is not null
    group by station_group
), group_lines as (
    select
        d.station_group,
        string_agg(distinct f.line_name, ', ' order by f.line_name) as line_names
    from {{ ref('fct_station_daily') }} as f
    inner join {{ ref('dim_station') }} as d on f.station_code = d.station_code
    where d.station_group is not null
    group by d.station_group
)
select
    a.station_group,
    a.station_codes,
    a.station_code_count,
    l.line_names,
    a.districts,
    a.first_seen_date,
    a.last_seen_date
from group_attributes as a
left join group_lines as l on a.station_group = l.station_group
