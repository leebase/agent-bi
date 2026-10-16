Source: https://docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec

# Semantic-view YAML interoperability fixture

The following vendor example is retained solely as a round-trip interoperability fixture.
Attribution: Snowflake documentation, retrieved 2026-10-14. Vendor example retains its original rights; see THIRD_PARTY_NOTICES.md. Full reference at the source URL above.

    name: revenue_analysis
    description: "Semantic view for analyzing revenue across products and customers"

    tables:
      - name: customers
        description: "Customer information"
        base_table:
          database: sales_db
          schema: public
          table: customers
        dimensions:
          - name: customer_name
            synonyms: ["client name", "customer"]
            description: "Full name of the customer"
            expr: c_name
            data_type: VARCHAR
            tags:
              - name:
                  database: sales_db
                  schema: public
                  tag: pii_type
                value: "name"
          - name: customer_segment
            synonyms: ["segment", "market segment"]
            description: "Customer market segment"
            expr: c_mktsegment
            data_type: VARCHAR
            is_enum: true

      - name: orders
        description: "Order information"
        base_table:
          database: sales_db
          schema: public
          table: orders
        dimensions:
          - name: order_date
            description: "Date when order was placed"
            expr: o_orderdate
            data_type: DATE
        time_dimensions:
          - name: order_year
            description: "Year when order was placed"
            expr: YEAR(o_orderdate)
            data_type: NUMBER
        facts:
          - name: order_total
            description: "Total order amount"
            expr: o_totalprice
            data_type: NUMBER
        metrics:
          - name: total_orders
            description: "Total number of orders"
            expr: COUNT(*)
          - name: average_order_value
            description: "Average order value"
            expr: AVG(o_totalprice)

    relationships:
      - name: orders_to_customers
        left_table: orders
        right_table: customers
        relationship_columns:
          - left_column: o_custkey
            right_column: c_custkey

    variables:
      - name: min_order_amount
        data_type: NUMBER
        default_value: "100"
        description: "Minimum order amount to include"

    metrics:
      - name: revenue_per_customer
        description: "Average revenue per customer"
        expr: orders.total_revenue / customers.customer_count
        access_modifier: public_access

    module_custom_instructions:
      sql_generation: |
        Always use fiscal year (April-March) for date grouping
        unless the user explicitly asks for calendar year.
      question_categorization: |
        Classify questions about order totals and revenue as
        financial questions.

    verified_queries:
      - name: top_customers_by_revenue
        question: "Who are the top 10 customers by revenue?"
        sql: |
          SELECT
            customer_name,
            SUM(order_total) as total_revenue
          FROM revenue_analysis
          GROUP BY customer_name
          ORDER BY total_revenue DESC
          LIMIT 10
        use_as_onboarding_question: true

    tags:
      - name:
          database: sales_db
          schema: public
          tag: department
        value: "sales"

## Creating a semantic view from YAML

See the linked upstream reference.
