select ride_date, scope, line_name, count(*) as row_count
from {{ ref('int_daily_volume_anomaly_calendar') }}
group by ride_date, scope, line_name
having count(*) > 1
