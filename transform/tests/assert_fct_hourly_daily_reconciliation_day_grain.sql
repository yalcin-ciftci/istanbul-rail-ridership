select ride_date, count(*) as row_count
from {{ ref('fct_hourly_daily_reconciliation_day') }}
group by ride_date
having ride_date is null or count(*) <> 1
