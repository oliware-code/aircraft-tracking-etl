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
    CONSTRAINT states_pk PRIMARY KEY ("timestamp", icao24)
);
CREATE INDEX idx_states_callsign_norm ON public.states USING btree (TRIM(BOTH FROM upper(callsign)));
CREATE INDEX idx_states_icao24_norm_ts ON public.states USING btree (TRIM(BOTH FROM lower(icao24)), "timestamp");

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
