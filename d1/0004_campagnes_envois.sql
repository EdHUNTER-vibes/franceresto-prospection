-- Campagnes d'envoi depuis le back-office, services d'envoi, suivi des e-mails, base des pros de l'annuaire.
-- Rejouable : les ALTER TABLE déjà appliqués (« duplicate column name ») sont ignorés par fichier_r2.py.

-- Établissements (CHR) et pros de l'annuaire dans la même table.
ALTER TABLE etablissements ADD COLUMN cible TEXT NOT NULL DEFAULT 'chr';  -- chr, annuaire
ALTER TABLE etablissements ADD COLUMN specialites TEXT;                   -- annuaire : JSON des spécialités (slugs)
ALTER TABLE etablissements ADD COLUMN score_chr INTEGER;                  -- annuaire : indices de travail avec la restauration
CREATE INDEX IF NOT EXISTS etab_cible ON etablissements (cible, departement, actif);

-- Campagnes : contenu, ciblage, volume et plages horaires.
ALTER TABLE campagnes ADD COLUMN cible TEXT NOT NULL DEFAULT 'chr';       -- chr, annuaire
ALTER TABLE campagnes ADD COLUMN objet TEXT;
ALTER TABLE campagnes ADD COLUMN contenu_html TEXT;                       -- champs {{etablissement}}, {{commune}}, {{specialite}}
ALTER TABLE campagnes ADD COLUMN contenu_texte TEXT;
ALTER TABLE campagnes ADD COLUMN expediteur_nom TEXT;
ALTER TABLE campagnes ADD COLUMN repondre_a TEXT;
ALTER TABLE campagnes ADD COLUMN nominatifs INTEGER NOT NULL DEFAULT 0;   -- 1 : adresses nominatives incluses
ALTER TABLE campagnes ADD COLUMN total_max INTEGER;                       -- nombre maximal d'envois (NULL : toute la cible)
-- JSON, heure de Paris : [{"jours": [1, 2, 3, 4, 5], "debut": "09:00", "fin": "11:00"}, …] (1 = lundi)
ALTER TABLE campagnes ADD COLUMN plages TEXT;
ALTER TABLE campagnes ADD COLUMN lien TEXT;                               -- page d'arrivée
ALTER TABLE campagnes ADD COLUMN code TEXT;                               -- code de campagne (attribution des inscriptions)
ALTER TABLE campagnes ADD COLUMN debut_le TEXT;                           -- pas d'envoi avant cette date
ALTER TABLE campagnes ADD COLUMN modifiee_le TEXT;

-- Envois : service utilisé et suivi.
ALTER TABLE envois ADD COLUMN fournisseur TEXT;
ALTER TABLE envois ADD COLUMN delivre_le TEXT;
ALTER TABLE envois ADD COLUMN ouvert_le TEXT;
ALTER TABLE envois ADD COLUMN clique_le TEXT;
ALTER TABLE envois ADD COLUMN erreur TEXT;
CREATE INDEX IF NOT EXISTS envois_message ON envois (message_id);
CREATE INDEX IF NOT EXISTS envois_date ON envois (envoye_le);

-- Services d'envoi : un service n'est activé qu'après l'accord écrit de l'hébergeur pour la prospection.
CREATE TABLE IF NOT EXISTS fournisseurs (
  nom         TEXT PRIMARY KEY,            -- ex. brevo
  type        TEXT NOT NULL CHECK (type IN ('brevo')),
  quota_jour  INTEGER NOT NULL,            -- envois de prospection par jour au plus
  actif       INTEGER NOT NULL DEFAULT 0,
  expediteur  TEXT,                        -- adresse d'envoi (domaine de prospection)
  priorite    INTEGER NOT NULL DEFAULT 1,
  modifie_le  TEXT
);

-- Événements reçus des services (délivré, ouvert, cliqué, erreur, plainte, désinscription).
CREATE TABLE IF NOT EXISTS evenements (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  date        TEXT NOT NULL,
  fournisseur TEXT,
  message_id  TEXT,
  email       TEXT,
  type        TEXT NOT NULL,
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS evenements_message ON evenements (message_id);
