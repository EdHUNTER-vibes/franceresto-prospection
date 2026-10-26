-- Listes d'adresses venues d'ailleurs (listes de prospection faites à la main). Conservée telle quelle pour
-- garder l'origine de chaque adresse et ce qui a déjà été envoyé ; rattachée ensuite aux établissements
-- SIRENE par scripts/rattacher_liste.py. Rejouable.
CREATE TABLE IF NOT EXISTS listes_externes (
  email           TEXT PRIMARY KEY,      -- en minuscules
  liste           TEXT NOT NULL,         -- nom de la liste
  departement     TEXT,                  -- tel que donné par la liste (NULL si inconnu)
  commune         TEXT,
  nom             TEXT,
  statut_envoi    TEXT NOT NULL CHECK (statut_envoi IN ('envoye', 'non_envoye', 'inconnu')),
  douteux         INTEGER NOT NULL DEFAULT 0,  -- localisation signalée « à vérifier » dans la liste
  importe_le      TEXT NOT NULL,
  siret           TEXT,                  -- établissement rattaché (SIRET, ou « ext:… » sans SIRET)
  rattachement    TEXT,                  -- domaine, nom, aucun
  rattache_le     TEXT
);
CREATE INDEX IF NOT EXISTS listes_a_rattacher ON listes_externes (rattache_le);
