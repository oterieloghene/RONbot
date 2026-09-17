-- state_a/state_b stored alphabetically -- cogs/cars.py looks these up in
-- both directions, so travel time and cooldown are the same regardless of
-- which way you're going.
--
-- Abuja <-> Delta: 20 min at 60 km/h        -> 20 km,  10h cooldown
-- Delta <-> Lagos: 10 min at 60 km/h        -> 10 km,  5h cooldown
-- Abuja <-> Lagos: 10 min at 60 km/h        -> 10 km,  10h cooldown
INSERT INTO interstate_routes (state_a, state_b, distance_km, cooldown_hours) VALUES
('ABUJA', 'DELTA', 20, 10),
('DELTA', 'LAGOS', 10, 5),
('ABUJA', 'LAGOS', 10, 10)
ON CONFLICT (state_a, state_b) DO NOTHING;
