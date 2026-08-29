SELECT DISTINCT
    token_mint_address AS token_address,
    block_time AS observed_at,
    from_owner,
    to_owner,
    CAST(amount_display AS DOUBLE) AS amount,
    amount_usd,
    tx_id,
    block_slot,
    'tokens_solana.transfers' AS source
FROM tokens_solana.transfers
WHERE block_time >= TIMESTAMP '{{month_start}}'
  AND block_time < TIMESTAMP '{{month_end}}'
  AND outer_executing_account IN (
      '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P',
      'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
  )
ORDER BY block_time, block_slot, tx_id, token_mint_address
