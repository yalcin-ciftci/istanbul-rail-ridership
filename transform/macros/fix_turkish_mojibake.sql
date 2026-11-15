{% macro fix_turkish_mojibake(col) -%}
    replace(replace(replace(replace(replace(replace(
        {{ col }}, 'ý', 'ı'), 'Ý', 'İ'), 'þ', 'ş'), 'Þ', 'Ş'), 'ð', 'ğ'), 'Ð', 'Ğ')
{%- endmacro %}
