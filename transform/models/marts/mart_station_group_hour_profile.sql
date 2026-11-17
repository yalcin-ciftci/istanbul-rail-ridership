{{ config(alias='fct_station_group_hour_profile') }}

select
    station_group, day_type, ride_hour, typical_passage, peak_reference,
    relative_index, band, baseline_days, baseline_start, baseline_end,
    case
        when baseline_days < 20 then 'few_days'
        when peak_reference < 50 then 'low_volume'
        else 'ok'
    end as reliability
from {{ ref('fct_station_group_hour_profile') }}
