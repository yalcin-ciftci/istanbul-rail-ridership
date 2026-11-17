select column_name, data_type
from information_schema.columns
where table_catalog = current_database()
  and table_schema = '{{ ref("int_daily_volume_anomaly").schema }}'
  and table_name = '{{ ref("int_daily_volume_anomaly").identifier }}'
  and column_name in ('journey_count', 'journey_count_estimated')
  and data_type <> 'BIGINT'
