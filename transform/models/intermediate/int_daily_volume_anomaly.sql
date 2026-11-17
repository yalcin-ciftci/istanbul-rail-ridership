{{ config(materialized='table') }}

with line_daily as (
    select
        ride_date,
        line_name,
        journey_count,
        journey_count_estimated,
        has_suspect_journeys
    from {{ ref('fct_line_daily') }}
), scoped_daily as (
    select
        ride_date,
        'line' as scope,
        line_name,
        journey_count,
        journey_count_estimated,
        has_suspect_journeys
    from line_daily

    union all

    select
        ride_date,
        'system' as scope,
        '*' as line_name,
        cast(sum(journey_count) as bigint) as journey_count,
        cast(sum(journey_count_estimated) as bigint) as journey_count_estimated,
        bool_or(has_suspect_journeys) as has_suspect_journeys
    from line_daily
    group by ride_date
), history as (
    select
        ride_date,
        scope,
        line_name,
        journey_count,
        journey_count_estimated,
        has_suspect_journeys,
        min(ride_date) over (partition by scope, line_name) as first_seen_date,
        count(journey_count_estimated) over previous_weeks as baseline_weeks,
        median(journey_count_estimated) over previous_weeks as history_median
    from scoped_daily
    window previous_weeks as (
        partition by scope, line_name, dayofweek(ride_date)
        order by ride_date
        range between interval 56 day preceding and interval 7 day preceding
    )
), baseline as (
    select
        ride_date,
        scope,
        line_name,
        journey_count,
        journey_count_estimated,
        has_suspect_journeys,
        first_seen_date,
        case when baseline_weeks >= 4 then history_median end as baseline_journeys,
        baseline_weeks
    from history
), ratios as (
    select
        ride_date,
        scope,
        line_name,
        journey_count,
        journey_count_estimated,
        has_suspect_journeys,
        first_seen_date,
        baseline_journeys,
        baseline_weeks,
        journey_count / nullif(baseline_journeys, 0) as ratio_to_baseline,
        journey_count_estimated / nullif(baseline_journeys, 0) as ratio_estimated_to_baseline
    from baseline
)
select
    ride_date,
    scope,
    line_name,
    journey_count,
    journey_count_estimated,
    has_suspect_journeys,
    baseline_journeys,
    baseline_weeks,
    ratio_to_baseline,
    ratio_estimated_to_baseline,
    case
        when baseline_journeys is null then 'no_baseline'
        when scope = 'line' and ride_date < first_seen_date + interval 180 day then 'new_line'
        when scope = 'line' and baseline_journeys < 5000 then 'low_volume'
        when ratio_estimated_to_baseline >= 1.6 then 'spike'
        when ratio_estimated_to_baseline <= 0.35 then 'drop'
        else 'none'
    end as anomaly
from ratios
