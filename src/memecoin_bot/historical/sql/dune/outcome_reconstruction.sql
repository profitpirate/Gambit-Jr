-- One transaction-level price observation after aggregating Dune route segments.
WITH raw AS (
    SELECT
        CASE WHEN token_bought_mint_address != 'So11111111111111111111111111111111111111112'
             THEN token_bought_mint_address ELSE token_sold_mint_address END AS token_address,
        block_time AS observed_at,
        CASE WHEN token_bought_mint_address != 'So11111111111111111111111111111111111111112'
             THEN token_bought_amount ELSE token_sold_amount END AS token_amount,
        amount_usd, tx_id, block_slot
    FROM dex_solana.trades
    WHERE block_time >= TIMESTAMP '{{month_start}}'
      AND block_time < TIMESTAMP '{{month_end}}'
      AND project_program_id IN (
          '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P',
          'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
      )
), aggregated AS (
    SELECT token_address, observed_at, SUM(token_amount) AS token_amount,
           SUM(amount_usd) AS amount_usd, tx_id, block_slot
    FROM raw
    GROUP BY token_address, observed_at, tx_id, block_slot
)
SELECT token_address, observed_at,
       CASE WHEN token_amount > 0 THEN amount_usd / token_amount ELSE NULL END AS price_usd,
       amount_usd, tx_id, block_slot, 'dex_solana.trades:outcome_path' AS source
FROM aggregated
ORDER BY observed_at, block_slot, tx_id, token_address
