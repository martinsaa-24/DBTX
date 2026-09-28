select
    stores.store_name,
    cast(date_trunc('month', orders.ordered_at) as date) as order_month,
    count(*) as order_count,
    sum(orders.subtotal) as revenue,
    sum(orders.tax_paid) as tax_paid
from {{ ref('orders') }} as orders
join {{ ref('stg_stores') }} as stores using (store_id)
group by 1, 2
