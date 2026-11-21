-- Everest structure only. Story text stays in adventurer/challenges.py.
-- Heights are the current foot values times 0.3048, stored at numeric(12, 5),
-- so a later percentage is the same height the site uses today.
-- position starts at 0 and follows the order in challenges.py.

INSERT INTO adventurer.expeditions (slug, name, target_elevation_m, status)
VALUES (
    'everest',
    'Everest',
    (29032::numeric * 0.3048)::numeric(12, 5),
    'active'
);

INSERT INTO adventurer.expedition_checkpoints (
    expedition_id, slug, name, position, elevation_m
)
SELECT
    expedition.id,
    stop.slug,
    stop.name,
    stop.position,
    (stop.elevation_ft * 0.3048)::numeric(12, 5)
FROM adventurer.expeditions AS expedition
CROSS JOIN (
    VALUES
        (0, 'timms-hill', 'Timms Hill', 1951.5::numeric),
        (1, 'four-thousand', '4,000 feet', 4000::numeric),
        (2, 'seven-thousand', '7,000 feet', 7000::numeric),
        (3, 'lukla', 'Lukla', 9383::numeric),
        (4, 'namche', 'Namche Bazaar', 11286::numeric),
        (5, 'tengboche', 'Tengboche', 12664::numeric),
        (6, 'dingboche', 'Dingboche', 14469::numeric),
        (7, 'lobuche', 'Lobuche', 16207::numeric),
        (8, 'gorak-shep', 'Gorak Shep', 16942::numeric),
        (9, 'base-camp', 'Everest Base Camp', 17598::numeric),
        (10, 'kala-patthar', 'Kala Patthar', 18520::numeric),
        (11, 'camp-1', 'Camp I', 19898::numeric),
        (12, 'camp-2', 'Camp II', 20997::numeric),
        (13, 'camp-3', 'Camp III', 23622::numeric),
        (14, 'south-col', 'South Col', 26083::numeric),
        (15, 'summit', 'Summit', 29032::numeric)
) AS stop (position, slug, name, elevation_ft)
WHERE expedition.slug = 'everest';
