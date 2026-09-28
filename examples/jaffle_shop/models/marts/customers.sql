with order_stats as (
    select
        customer_id,
        min(ordered_at) as first_ordered_at,
        max(ordered_at) as last_ordered_at,
        count(*) as order_count,
        sum(order_total) as lifetime_spend
    from {{ ref('orders') }}
    group by 1
)

select
    customers.customer_id,
    customers.customer_name,
    order_stats.first_ordered_at,
    order_stats.last_ordered_at,
    coalesce(order_stats.order_count, 0) as order_count,
    coalesce(order_stats.lifetime_spend, 0) as lifetime_spend,
    case
        when order_stats.order_count is null then 'prospect'
        when order_stats.order_count = 1 then 'new'
        else 'returning'
    end as customer_type
from {{ ref('stg_customers') }} as customers
left join order_stats using (customer_id)
