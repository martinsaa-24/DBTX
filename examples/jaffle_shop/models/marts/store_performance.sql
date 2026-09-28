select
    stores.store_name,
    stores.opened_at,
    count(*) as order_count,
    count(distinct orders.customer_id) as customer_count,
    sum(orders.subtotal) as revenue,
    cast(sum(orders.subtotal) / count(*) as decimal(12, 2)) as avg_order_value
from {{ ref('orders') }} as orders
join {{ ref('stg_stores') }} as stores using (store_id)
group by 1, 2
