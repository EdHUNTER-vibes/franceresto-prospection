# France Resto – prospection

Scripts qui constituent la base de prospects de [France Resto](https://franceresto.net) : les établissements CHR (cafés, hôtels, restaurants) de France métropolitaine et leurs adresses e-mail **publiées**.

Ce dépôt contient seulement du code. Aucune donnée n'y est enregistrée :
- les données sont dans un stockage privé (Cloudflare R2 et D1) ;
- les accès sont dans les secrets du dépôt.

## Sources
Seules des sources ouvertes ou publiées par les établissements eux-mêmes sont utilisées :
- **SIRENE** (Insee, Licence Ouverte) :
  - les établissements actifs des codes NAF 55.10Z, 56.10A, 56.10B, 56.10C, 56.21Z et 56.30Z ;
  - sans les entreprises en diffusion partielle, qui se sont opposées à l'usage commercial de leurs données ;
  - jamais le nom d'un entrepreneur individuel.
- **OpenStreetMap** (ODbL, usage interne) : site web et e-mail publiés sur les lieux CHR.
- **DATAtourisme** (Licence Ouverte) : site web publié par les offices de tourisme.
- **Le site de l'établissement** :
  - les pages contact, mentions légales, réservation et recrutement ;
  - `robots.txt` est respecté ;
  - l'agent est identifié (`FranceRestoBot`) ;
  - une page à la fois par site, avec une seconde d'attente entre deux pages.

Aucune récupération sur les moteurs de recherche, les annuaires, les plateformes de réservation ou de livraison, ni les réseaux sociaux.

## Règles
- **Aucune adresse inventée ni devinée.** Une adresse n'est retenue que si elle est publiée, et seulement si elle est :
  - soit du domaine du site de l'établissement ;
  - soit d'une messagerie grand public.
- **Domaines vérifiés** par le DNS. Aucun e-mail n'est envoyé pour vérifier une adresse.
- **Origine de chaque adresse conservée** : source, page exacte et date de consultation.
- **Site trouvé par son nom** (`scripts/rechercher_sites.py`) : il n'est retenu que si ses pages citent le SIREN ou l'adresse de l'établissement.
- **Refus et désinscriptions conservés sans limite** et respectés par tous les traitements.

## Organisation
| Élément | Rôle |
|---|---|
| `scripts/` | Import, lecture des sites, recherche des sites, rattachement des listes, synchronisation |
| `d1/` | Schéma de la base (fichier de travail SQLite et Cloudflare D1) |
| `.github/workflows/` | Traitements lancés à la main, et synchronisation chaque nuit |

**Le fichier de travail** est une base SQLite complète, gardée dans un bucket R2 privé (`prospects.sqlite.gz`, avec 7 archives tournantes). Chaque traitement :
1. le récupère ;
2. récupère dans D1 les campagnes, envois et exclusions ;
3. fait son travail ;
4. renvoie le fichier sur R2 ;
5. écrit dans D1 seulement les différences : les établissements qui ont une adresse et les adresses elles-mêmes.

Tous les traitements partagent la file `prospection-d1` : ils ne tournent jamais en même temps.

**Passages enchaînés** : la lecture et la recherche des sites travaillent par passages de durée limitée.
- Chacun enregistre ce qu'il a fait, puis relance le suivant tant qu'il reste du travail (option `enchainer`).
- GitHub ne garde qu'un seul traitement en attente par file : lancer une autre chaîne en même temps annulerait la relance en attente.

**Limite de D1** : si la limite quotidienne d'écritures de D1 est atteinte, le traitement s'arrête proprement. Le fichier est déjà enregistré sur R2, et la synchronisation de nuit reprend.

**Secrets** :
- `CLOUDFLARE_API_TOKEN` (D1 : modifier) ;
- `R2_PROSPECTS_ACCESS_KEY_ID` et `R2_PROSPECTS_SECRET_ACCESS_KEY` (jeton R2 limité au bucket) ;
- `R2_ACCOUNT_ID`, en variable ou en secret.

**Restaurer une archive** : télécharger `archives/prospects-<jour>.sqlite.gz` depuis le bucket, puis le déposer sous le nom `prospects.sqlite.gz`.
