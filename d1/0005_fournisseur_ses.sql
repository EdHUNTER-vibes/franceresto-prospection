-- Amazon SES comme service d'envoi (accès en production accordé le 30/10/2026, région eu-north-1).
-- SQLite ne modifie pas une contrainte CHECK : table recréée, lignes conservées. Rejouable.
CREATE TABLE IF NOT EXISTS fournisseurs_nouveau (
  nom         TEXT PRIMARY KEY,            -- ex. brevo, ses
  type        TEXT NOT NULL CHECK (type IN ('brevo', 'ses')),
  quota_jour  INTEGER NOT NULL,            -- envois de prospection par jour au plus
  actif       INTEGER NOT NULL DEFAULT 0,
  expediteur  TEXT,                        -- adresse d'envoi (domaine de prospection)
  priorite    INTEGER NOT NULL DEFAULT 1,
  modifie_le  TEXT
);
INSERT OR IGNORE INTO fournisseurs_nouveau SELECT nom, type, quota_jour, actif, expediteur, priorite, modifie_le FROM fournisseurs;
DROP TABLE fournisseurs;
ALTER TABLE fournisseurs_nouveau RENAME TO fournisseurs;
