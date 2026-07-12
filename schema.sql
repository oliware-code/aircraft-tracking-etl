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
    CONSTRAINT aircraft_pkey PRIMARY KEY (icao24)
);

CREATE TABLE flight_routes (
    callsign text NOT NULL,
    operator text,
    iata_origin text,
    iata_destination text,
    icao_origin text,
    icao_destination text,
    CONSTRAINT flight_routes_pkey PRIMARY KEY (callsign)
);

CREATE TABLE states (
    timestamp bigint NOT NULL,
    icao24 text NOT NULL,
    callsign text,
    time_position bigint,
    last_contact bigint,
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
    CONSTRAINT states_pk PRIMARY KEY ("timestamp", icao24)
);
CREATE INDEX idx_states_callsign_norm ON public.states USING btree (TRIM(BOTH FROM upper(callsign)));
CREATE INDEX idx_states_icao24_norm_ts ON public.states USING btree (TRIM(BOTH FROM lower(icao24)), "timestamp");

-- Foreign keys
ALTER TABLE states
    ADD CONSTRAINT aircraft_fk FOREIGN KEY (icao24) REFERENCES aircraft(icao24);
ALTER TABLE states
    ADD CONSTRAINT callsign_fk FOREIGN KEY (callsign) REFERENCES flight_routes(callsign);
