{% macro repair_coordinate(col) -%}
    case
        when {{ col }} is null
            or {{ col }} = ''
            or not regexp_full_match({{ col }}, '^[0-9.]*[0-9][0-9.]*$')
            then cast(null as double)
        else cast(
            left(replace({{ col }}, '.', ''), 2)
            || '.' || substr(replace({{ col }}, '.', ''), 3)
            as double
        )
    end
{%- endmacro %}
