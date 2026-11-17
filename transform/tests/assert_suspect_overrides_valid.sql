select o.ride_date
from {{ ref('journey_suspect_overrides') }} as o
left join {{ ref('int_journey_quality_daily') }} as q on o.ride_date = q.ride_date
where q.ride_date is null or year(o.ride_date) <> 2022
