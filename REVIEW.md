# Revisione del 2 novembre 2026

Revisione statica degli script Python e dei flussi descritti nella documentazione,
con esecuzione della suite offline. Le integrazioni sono simulate dai test:
questa revisione non certifica la compatibilità con servizi o CLI reali aggiornati.

## Correzioni

| Priorità | Problema riprodotto | Correzione |
| --- | --- | --- |
| Alta | Sessioni come `a b`, `a-b` e `A-B`, o nomi non latini, producevano lo stesso percorso di riepilogo. Un push poteva pubblicare il contenuto di un'altra sessione. | Suffisso derivato dall'identità originale quando la normalizzazione modifica il nome; mantenuti i nomi semplici esistenti. |
| Alta | `pull` usava una fonte memorizzata localmente anche dopo la sostituzione da un'altra macchina o con `--notebook` diverso. | Verifica della fonte nell'elenco del notebook selezionato e ricerca della fonte corrente quando quella precedente manca. |
| Alta | Un ripristino con elementi malformati poteva interrompersi dopo aver già creato alcune sessioni. | Validazione preliminare di versione, struttura, slug, stato e contenitori prima di scrivere le sessioni. Gli errori di I/O durante la scrittura non sono transazionali. |
| Media | Il frontmatter importato trasformava stringhe come `true` e `001` in altri tipi e alterava backslash, newline o liste con virgole. | Stringhe serializzate con escaping JSON compatibile con YAML; lettura corrispondente anche senza PyYAML. |
| Media | `session-log verify` terminava con un traceback su elementi non validi in `tokens.json`. | Diagnostica `SL009`, esito di errore e output JSON leggibile anche per questi elementi. |

I sei nuovi test di regressione riproducevano i problemi prima delle correzioni.
Il test YAML verifica sia PyYAML sia il parser incorporato.

## Miglioramenti proposti

1. **Serializzare le operazioni di sincronizzazione.** *Parte locale risolta il
   3 novembre 2026:* `push` gira sotto un lock per vault
   (`.duel-vault/progress/.push.lock`), con attesa massima `--lock-wait` e test con
   processi concorrenti. Resta da definire una strategia esplicita per scegliere lo
   snapshot più recente fra più macchine, che il lock locale non coordina. La ricerca
   attuale, quando manca la fonte precedente, sceglie l'ultima fonte compatibile
   nell'elenco della CLI: non è una garanzia cronologica. Strategia proposta, con le
   decisioni aperte: [`PROPOSTA-MULTI-MACCHINA.md`](PROPOSTA-MULTI-MACCHINA.md).
2. **Rafforzare gli schemi persistiti e la validazione della configurazione.**
   Centralizzare i controlli su budget, intervalli, elementi pending e versioni dei
   file. La correzione di `verify` riguarda gli elementi del registro token;
   non rende ogni comando tollerante a qualsiasi JSON danneggiato. Introdurre
   migrazioni esplicite e test per input errati prima di cambiare i formati.
3. **Aggiungere CI e test di compatibilità.** Eseguire la suite sulle versioni Python
   supportate, con e senza PyYAML; aggiungere verifiche dedicate a Windows per lock,
   processi distaccati e notifiche. Gli eseguibili simulati attuali verificano il
   flusso applicativo, ma non rilevano cambiamenti dei contratti delle CLI esterne.
4. **Chiarire le due modalità NotebookLM nella guida iniziale.** La generazione
   da fonti richiede opt-in, mentre la pubblicazione dell'avanzamento è abilitata
   per default. Rendere evidente questa distinzione e l'opzione
   `progress_notebook.enabled` nella configurazione iniziale.

## Verifica riproducibile

```bash
python3 -m unittest discover -s tests -v
git diff --check
```

La suite iniziale comprendeva 33 test; con le regressioni ne comprende 39.
