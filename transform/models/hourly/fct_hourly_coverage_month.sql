with classified as (
    select
        h.ride_date, h.ride_hour, h.line_code, h.passage_count,
        h.station_name_raw,
        nullif(m.station_group, '') is not null as mapped,
        m.match_method,
        nullif(l.daily_line_name, '') is not null as in_daily_scope
    from {{ ref('stg_rail_hourly') }} as h
    left join {{ ref('hourly_station_map') }} as m
        on h.line_code = m.line_code and h.station_key = m.station_key
    left join {{ ref('hourly_line_map') }} as l on h.line_code = l.line_code
), monthly as (
    select
        strftime(ride_date, '%Y-%m') as month,
        case when grouping(line_code) = 1 then 'ALL' else line_code end as line_code,
        day(last_day(min(ride_date))) as days_in_month,
        count(distinct ride_date) as days_present,
        count(distinct (ride_date, ride_hour)) as hours_present,
        cast(count(*) as bigint) as source_rows,
        cast(coalesce(sum(passage_count), 0) as bigint) as passage_count,
        cast(count(*) filter (where station_name_raw is null) as bigint) as null_station_rows,
        cast(coalesce(sum(passage_count) filter (where mapped), 0) as bigint) as mapped_passage,
        cast(coalesce(sum(passage_count) filter (
            where not mapped and station_name_raw is not null
                and coalesce(match_method, 'unmapped') <> 'ambiguous'
        ), 0) as bigint) as unmapped_passage,
        cast(coalesce(sum(passage_count) filter (where match_method = 'ambiguous'), 0) as bigint) as ambiguous_passage,
        bool_or(in_daily_scope) as in_daily_scope
    from classified
    group by grouping sets ((strftime(ride_date, '%Y-%m'), line_code), (strftime(ride_date, '%Y-%m')))
), with_history as (
    select
        c.month, c.line_code, c.days_in_month, c.days_present, c.hours_present,
        c.source_rows, c.passage_count, c.null_station_rows,
        c.mapped_passage, c.unmapped_passage, c.ambiguous_passage, c.in_daily_scope,
        (
            select median(previous.passage_count)
            from (
                select p.passage_count
                from monthly as p
                where p.line_code = c.line_code and p.month < c.month
                    and p.days_present = p.days_in_month
                order by p.month desc
                limit 12
            ) as previous
        ) as previous_median
    from monthly as c
), flagged as (
    select
        month, line_code, days_in_month, days_present, hours_present,
        source_rows, passage_count, null_station_rows,
        mapped_passage, unmapped_passage, ambiguous_passage,
        mapped_passage::double / nullif(passage_count, 0) as mapped_share,
        in_daily_scope,
        month between '2021-01' and '2024-10' as overlaps_daily_period,
        case
            when days_present < days_in_month then 'partial_days'
            when passage_count < previous_median * 0.5 then 'low_volume'
            else 'ok'
        end as quality_flag
    from with_history
), empty_months as (
    select
        month, '*' as line_code,
        day(last_day(cast(month || '-01' as date))) as days_in_month,
        0 as days_present, 0 as hours_present, 0::bigint as source_rows,
        0::bigint as passage_count, 0::bigint as null_station_rows,
        0::bigint as mapped_passage, 0::bigint as unmapped_passage, 0::bigint as ambiguous_passage,
        cast(null as double) as mapped_share,
        false as in_daily_scope,
        month between '2021-01' and '2024-10' as overlaps_daily_period,
        'empty' as quality_flag
    from {{ source('raw_hourly', 'hourly_transport_months') }}
    where status = 'empty'
)
select
    month, line_code, days_in_month, days_present, hours_present, source_rows,
    passage_count, null_station_rows, mapped_passage, unmapped_passage,
    ambiguous_passage, mapped_share, in_daily_scope, overlaps_daily_period, quality_flag
from flagged
union all
select
    month, line_code, days_in_month, days_present, hours_present, source_rows,
    passage_count, null_station_rows, mapped_passage, unmapped_passage,
    ambiguous_passage, mapped_share, in_daily_scope, overlaps_daily_period, quality_flag
from empty_months
