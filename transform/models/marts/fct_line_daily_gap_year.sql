with gaps as (
    select cast(substr(month, 1, 4) as integer) as year, daily_line_name,
        count(*) as missing_months, sum(hourly_passage) as hourly_passage_in_gap
    from {{ ref('fct_line_daily_coverage_month') }}
    where status in ('missing_in_daily', 'partial_in_daily')
        and month between '2021-01' and '2024-10'
    group by cast(substr(month, 1, 4) as integer), daily_line_name
), daily_year as (
    select year(ride_date) as year, sum(journey_count_estimated) as daily_year_total
    from {{ ref('fct_line_daily') }}
    where year(ride_date) between 2021 and 2024
    group by year(ride_date)
)
select g.year, g.daily_line_name, g.missing_months, g.hourly_passage_in_gap,
    d.daily_year_total,
    g.hourly_passage_in_gap / nullif(d.daily_year_total, 0) as gap_share_of_daily
from gaps as g
left join daily_year as d on g.year = d.year
