-- Schema for the OpenSky aircraft-tracking database
-- Generated from the live PostgreSQL instance (schema only, no data)

CREATE TABLE aircraft (
    icao24 text NOT NULL,
    origin_country text,
    registration text,
    friendly_name text,
    aircraft_type text,
    icao_aircraft_type text,
    manufacturer text,
    registered_owner_country_iso_name text,
    enrichment_checked_at timestamptz,
    CONSTRAINT aircraft_pkey PRIMARY KEY (icao24)
);

CREATE TABLE flight_routes (
    callsign text NOT NULL,
    operator text,
    iata_origin text,
    iata_destination text,
    icao_origin text,
    icao_destination text,
    enrichment_checked_at timestamptz,
    CONSTRAINT flight_routes_pkey PRIMARY KEY (callsign)
);

-- states is partitioned BY RANGE(timestamp), one partition per day
-- (states_YYYY_MM_DD). partition_maintenance.py creates new partitions ahead
-- of time and detaches partitions older than its --retention-days into the
-- `archive` schema (DETACH PARTITION + SET SCHEMA archive) -- never DROP, so
-- older data is always still queryable at archive.states_YYYY_MM_DD, just
-- out of the live table's partition-pruning path.
--
-- Constraint/index names below carry the "states_daily_*" prefix as a
-- cosmetic leftover from the migration that created this table (see
-- DAILY_PARTITIONING_MIGRATION.txt) -- harmless, not renamed, out of scope
-- for that migration.
CREATE TABLE states (
    timestamp timestamptz NOT NULL,
    icao24 text NOT NULL,
    callsign text,
    time_position timestamptz,
    last_contact timestamptz,
    longitude numeric,
    latitude numeric,
    barometric_altitude numeric,
    on_ground boolean,
    ground_speed numeric,
    true_track numeric,
    vertical_rate numeric,
    sensors integer[],
    geo_altitude numeric,
    squawk numeric,
    spi boolean,
    position_source smallint,
    CONSTRAINT states_daily_pk PRIMARY KEY ("timestamp", icao24)
) PARTITION BY RANGE ("timestamp");
CREATE INDEX idx_states_daily_callsign_norm ON public.states USING btree (TRIM(BOTH FROM upper(callsign)));
CREATE INDEX idx_states_daily_icao24_norm_ts ON public.states USING btree (TRIM(BOTH FROM lower(icao24)), "timestamp");

-- Daily partitions themselves (states_2026_07_03 ... states_2026_07_24 as of
-- this writing) are created/archived dynamically by partition_maintenance.py
-- and are not enumerated here -- see that script and
-- DAILY_PARTITIONING_MIGRATION.txt for the current live set.

CREATE TABLE airlines (
    icao text NOT NULL,
    iata text,
    name text,
    radio_callsign text,
    country text,
    country_iso text,
    CONSTRAINT airlines_pkey PRIMARY KEY (icao)
);

CREATE TABLE airports (
    iata text NOT NULL,
    icao text,
    name text,
    municipality text,
    country text,
    country_iso text,
    latitude numeric,
    longitude numeric,
    elevation integer,
    CONSTRAINT airports_pkey PRIMARY KEY (iata)
);

CREATE TABLE approach_alerts_sent (
    icao24 text NOT NULL,
    flight_started_at timestamptz NOT NULL,
    notified_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT approach_alerts_sent_pkey PRIMARY KEY (icao24)
);

-- Foreign keys
ALTER TABLE states
    ADD CONSTRAINT aircraft_fk FOREIGN KEY (icao24) REFERENCES aircraft(icao24);
ALTER TABLE states
    ADD CONSTRAINT callsign_fk FOREIGN KEY (callsign) REFERENCES flight_routes(callsign);
