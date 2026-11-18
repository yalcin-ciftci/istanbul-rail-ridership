with daily_year as (
    select year(ride_date) as year, line_name,
        sum(journey_count_estimated) as daily_journey_estimated
    from {{ ref('fct_line_daily') }}
    group by year(ride_date), line_name
), gap_year as (
    select year, daily_line_name,
        sum(hourly_passage_in_gap) as gap_lower_bound
    from {{ ref('fct_line_daily_gap_year') }}
    group by year, daily_line_name
), ratios as (
    select r.year, r.line_code, m.daily_line_name, r.value as report_value,
        d.daily_journey_estimated,
        d.daily_journey_estimated / nullif(r.value, 0) as ratio_estimated,
        coalesce(g.gap_lower_bound, 0) as gap_lower_bound,
        (coalesce(d.daily_journey_estimated, 0) + coalesce(g.gap_lower_bound, 0))
            / nullif(r.value, 0) as ratio_with_gap
    from {{ ref('external_annual_totals') }} as r
    left join {{ ref('external_line_map') }} as m on r.line_code = m.line_code
    left join daily_year as d
        on r.year = d.year and m.daily_line_name = d.line_name
    left join gap_year as g
        on r.year = g.year and m.daily_line_name = g.daily_line_name
    where r.operator = 'metro_istanbul' and r.measure = 'annual_passengers'
        and r.line_code is not null
)
select year, line_code, daily_line_name, report_value, daily_journey_estimated,
    ratio_estimated, gap_lower_bound, ratio_with_gap,
    case
        when daily_journey_estimated is null then 'missing_in_daily'
        when ratio_with_gap between 0.95 and 1.05 then 'ok'
        when ratio_with_gap < 0.95 then 'low'
        when ratio_with_gap > 1.05 then 'high'
    end as status
from ratios
