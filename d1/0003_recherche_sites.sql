-- Recherche des sites manquants (scripts/rechercher_sites.py). Fichier de travail seulement :
-- cette table n'est pas recopiée dans D1. Rejouable.
CREATE TABLE IF NOT EXISTS recherche_sites (
  siret       TEXT PRIMARY KEY,
  essaye_le   TEXT NOT NULL,
  domaines    INTEGER NOT NULL,        -- domaines essayés
  site        TEXT,                    -- site retenu, sinon NULL
  preuve      TEXT                     -- siren, adresse
);
