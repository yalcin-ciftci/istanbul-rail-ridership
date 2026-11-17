with hourly_totals as (
    select h.ride_date, h.station_group, h.ride_hour,
        sum(h.passage_count) as passage_count
    from {{ ref('fct_station_group_hourly') }} as h
    inner join {{ ref('fct_hourly_coverage_month') }} as c
        on strftime(h.ride_date, '%Y-%m') = c.month and c.line_code = 'ALL'
    where h.ride_date between date '2023-01-01' and date '2024-07-31'
        and c.quality_flag = 'ok'
    group by h.ride_date, h.station_group, h.ride_hour
), baseline_dates as (
    select ride_date, station_group,
        case
            when isodow(ride_date) = 6 then 'saturday'
            when isodow(ride_date) = 7 then 'sunday'
            else 'weekday'
        end as day_type
    from hourly_totals
    group by ride_date, station_group
    having sum(passage_count) > 0
), filled_hours as (
    select d.station_group, d.day_type, r.ride_hour,
        coalesce(h.passage_count, 0) as passage_count
    from baseline_dates as d
    cross join range(24) as r(ride_hour)
    left join hourly_totals as h
        on d.ride_date = h.ride_date and d.station_group = h.station_group
        and r.ride_hour = h.ride_hour
), typical as (
    select station_group, day_type, cast(ride_hour as smallint) as ride_hour,
        cast(median(passage_count) as double) as typical_passage,
        count(*) as baseline_days
    from filled_hours
    group by station_group, day_type, ride_hour
), peaks as (
    select station_group, day_type, ride_hour, typical_passage, baseline_days,
        max(typical_passage) over (partition by station_group) as peak_reference
    from typical
), relative as (
    select station_group, day_type, ride_hour, typical_passage, peak_reference,
        typical_passage / nullif(peak_reference, 0) as relative_index, baseline_days
    from peaks
)
select station_group, day_type, ride_hour, typical_passage, peak_reference, relative_index,
    case
        when relative_index <= 0.33 then 'quiet'
        when relative_index <= 0.66 then 'moderate'
        when relative_index > 0.66 then 'busy'
    end as band,
    baseline_days, date '2023-01-01' as baseline_start, date '2024-07-31' as baseline_end
from relative
