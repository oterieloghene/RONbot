-- Zones, zone->category mapping, and routes for all 3 states.
-- Run after schema_transportation.sql and locations_seed.sql.
-- Fare defaults to 100 on every route -- adjust directly in `routes` until
-- there's a command for Commerce/Finance to set it.

INSERT INTO zones (state, code, name) VALUES
('DELTA', 'A', 'Outskirts'),
('DELTA', 'B', 'Commerce & Mid Class'),
('DELTA', 'C', 'Government'),
('LAGOS', 'A', 'Ghetto'),
('LAGOS', 'B', 'Mainland'),
('LAGOS', 'C', 'Island'),
('ABUJA', 'A', 'Outskirts'),
('ABUJA', 'B', 'Commerce & Mid Class'),
('ABUJA', 'C', 'Government')
ON CONFLICT (state, code) DO NOTHING;

INSERT INTO zone_categories (zone_id, category)
SELECT z.id, v.category
FROM (VALUES
    -- Delta
    ('DELTA', 'A', 'LOW COST HOUSING'),
    ('DELTA', 'A', 'FARMS'),
    ('DELTA', 'B', 'MID CLASS HOUSING'),
    ('DELTA', 'B', 'BUSINESS & COMMERCE'),
    ('DELTA', 'B', 'CAREER HIGH SCHOOL'),
    ('DELTA', 'B', 'BORDER & ENTRY'),
    ('DELTA', 'B', 'STATE UNIVERSITY'),
    ('DELTA', 'B', 'BROADCASTING STATION'),
    ('DELTA', 'B', 'HOTEL & SUITES'),
    ('DELTA', 'C', 'HIGH CLASS HOUSING'),
    ('DELTA', 'C', 'STATE GOVERNMENT'),
    ('DELTA', 'C', 'GOVERNOR''S HOUSE'),
    ('DELTA', 'C', 'POLICE DEPARTMENT'),
    ('DELTA', 'C', 'GENERAL HOSPITAL'),
    ('DELTA', 'C', 'JUDICIARY'),
    ('DELTA', 'C', 'PROPERTY AND DEVELOPMENT DEPARTMENT'),
    ('DELTA', 'C', 'ELECTRICITY DISTRIBUTION COMPANY'),
    ('DELTA', 'C', 'BANK PLC'),

    -- Lagos
    ('LAGOS', 'A', 'GHETTO'),
    ('LAGOS', 'A', 'FARMS'),
    ('LAGOS', 'B', 'MAINLAND'),
    ('LAGOS', 'B', 'BUSINESS & COMMERCE'),
    ('LAGOS', 'B', 'CAREER HIGH SCHOOL'),
    ('LAGOS', 'B', 'BORDER & ENTRY'),
    ('LAGOS', 'B', 'DEFENCE ACADEMY'),
    ('LAGOS', 'C', 'ISLAND'),
    ('LAGOS', 'C', 'STATE GOVERNMENT'),
    ('LAGOS', 'C', 'GOVERNOR''S HOUSE'),
    ('LAGOS', 'C', 'POLICE DEPARTMENT'),
    ('LAGOS', 'C', 'GENERAL HOSPITAL'),
    ('LAGOS', 'C', 'JUDICIARY'),
    ('LAGOS', 'C', 'PROPERTY AND DEVELOPMENT DEPARTMENT'),
    ('LAGOS', 'C', 'ELECTRICITY DISTRIBUTION COMPANY'),
    ('LAGOS', 'C', 'BANK PLC'),

    -- Abuja / FCT
    ('ABUJA', 'A', 'LOW COST HOUSING'),
    ('ABUJA', 'A', 'FARMS'),
    ('ABUJA', 'B', 'MID CLASS HOUSING'),
    ('ABUJA', 'B', 'BUSINESS & COMMERCE'),
    ('ABUJA', 'B', 'CAREER HIGH SCHOOL'),
    ('ABUJA', 'B', 'BORDER & ENTRY'),
    ('ABUJA', 'B', 'UNIVERSITY OF BANKING AND MEDICINE'),
    ('ABUJA', 'B', 'HOTEL & SUITES'),
    ('ABUJA', 'B', 'TELEVISION AUTHORITY'),
    ('ABUJA', 'C', 'HIGH CLASS HOUSING'),
    ('ABUJA', 'C', 'ASO ROCK'),
    ('ABUJA', 'C', 'FEDERAL GOVERNMENT'),
    ('ABUJA', 'C', 'CENTRAL BANK OF NIGERIA'),
    ('ABUJA', 'C', 'POLICE DEPARTMENT'),
    ('ABUJA', 'C', 'GENERAL HOSPITAL'),
    ('ABUJA', 'C', 'JUDICIARY'),
    ('ABUJA', 'C', 'PROPERTY AND DEVELOPMENT DEPARTMENT'),
    ('ABUJA', 'C', 'ELECTRICITY DISTRIBUTION COMPANY'),
    ('ABUJA', 'C', 'BANK PLC')
) AS v(state, code, category)
JOIN zones z ON z.state = v.state AND z.code = v.code
ON CONFLICT (zone_id, category) DO NOTHING;

-- One route per zone pair, per state (A-B, B-C, A-C = 3 per state).
INSERT INTO routes (state, zone_a_id, zone_b_id, fare)
SELECT za.state, za.id, zb.id, 100
FROM zones za
JOIN zones zb ON za.state = zb.state AND za.code < zb.code
ON CONFLICT (state, zone_a_id, zone_b_id) DO NOTHING;
