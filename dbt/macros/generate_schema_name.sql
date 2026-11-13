{#
  dbt는 기본으로 profiles.yml의 schema와 모델의 +schema를 이어 붙입니다(gold_silver).
  이 프로젝트는 Iceberg 이름공간을 층 이름 그대로(silver, gold) 쓰므로 +schema를 그대로 씁니다.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
  {%- if custom_schema_name is none -%}
    {{ target.schema }}
  {%- else -%}
    {{ custom_schema_name | trim }}
  {%- endif -%}
{%- endmacro %}
