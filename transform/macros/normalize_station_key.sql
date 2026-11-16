{% macro normalize_station_key(col) -%}
    regexp_replace(upper(translate({{ col }}, 'İıŞşĞğÜüÖöÇç', 'IiSsGgUuOoCc')), '[^A-Z0-9]', '', 'g')
{%- endmacro %}
