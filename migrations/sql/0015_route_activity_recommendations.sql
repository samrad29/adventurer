CREATE TABLE adventurer.route_activity_recommendations (
    route_id bigint NOT NULL REFERENCES adventurer.routes (id) ON DELETE CASCADE,
    activity_type text NOT NULL CHECK (
        activity_type IN (
            'run', 'hike', 'walk', 'stair_climb', 'other', 'unspecified',
            'cycle', 'indoor_cycle'
        )
    ),
    position integer NOT NULL CHECK (position >= 0),
    PRIMARY KEY (route_id, activity_type),
    UNIQUE (route_id, position)
);
