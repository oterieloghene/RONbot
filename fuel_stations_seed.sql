-- Starting price/stock for each state's NNPC station. Both are
-- Commissioner-of-Petroleum-settable in-game (!set-fuel-price, !restock-fuel)
-- -- these are just what a fresh database starts with.
INSERT INTO fuel_stations (state, price_per_litre, stock_litres) VALUES
('DELTA', 200, 5000),
('LAGOS', 200, 5000),
('ABUJA', 200, 5000)
ON CONFLICT (state) DO NOTHING;
