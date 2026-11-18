with daily_year as (
    select year(ride_date) as year,
        sum(journey_count_estimated) as daily_journey_estimated,
        sum(journey_count) as daily_journey_count
    from {{ ref('fct_line_daily') }}
    where line_name not like 'TCDD%' and line_name not like 'IETT%'
    group by year(ride_date)
), gap_year as (
    select year, sum(hourly_passage_in_gap) as gap_lower_bound
    from {{ ref('fct_line_daily_gap_year') }}
    where daily_line_name not like 'TCDD%' and daily_line_name not like 'IETT%'
    group by year
)
select r.year, r.operator, r.value as report_value,
    coalesce(d.daily_journey_estimated, 0) as daily_journey_estimated,
    coalesce(d.daily_journey_count, 0) as daily_journey_count,
    coalesce(d.daily_journey_estimated, 0) / nullif(r.value, 0) as ratio_estimated,
    coalesce(d.daily_journey_count, 0) / nullif(r.value, 0) as ratio_source,
    coalesce(g.gap_lower_bound, 0) as gap_lower_bound,
    (coalesce(d.daily_journey_estimated, 0) + coalesce(g.gap_lower_bound, 0))
        / nullif(r.value, 0) as ratio_with_gap,
    r.source_url, r.source_page
from {{ ref('external_annual_totals') }} as r
left join daily_year as d on r.year = d.year
left join gap_year as g on r.year = g.year
where r.operator = 'metro_istanbul' and r.measure = 'annual_passengers'
