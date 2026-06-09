# Invoice Evidence Extract From Real Source

    - Evidence type: invoice
    - Source original file: originals/FACTU2015020048.jpg
    - Source traceability: original invoice image/PDF with dataset sidecar extraction
    - Extraction basis: GitHub dataset XML/TSV or PDF text extraction included in originals folder
    - Scenario note: clean three-way match, no duplicate hit

    ## Extracted Header Fields

    - Invoice number: FA02/2015/020059
    - Supplier legal name: Marc Demo
    - Invoice date: 2015-02-02
    - Due date: 2015-02-02
    - Related purchase order: BC06263
    - Currency: EUR
    - Untaxed amount: 75,974.00 EUR
    - Tax amount: 6,029.30 EUR
    - Total amount: 82,003.30 EUR
    - Supplier address: 3575  Buena Vista Avenue  Eugene COR 97401 États Unis

    ## Line Items From Source

    | description | quantity | unit_price | tax | subtotal |
    |---|---:|---:|---|---:|
    | Service Client (Heures Prépayées) | 64.0 Heures | 190.0 | TVA 20% | 12160.0 |
| Flipover | 35.0 Unités | 1700.0 | TVA 5,5% | 59500.0 |
| Combinaison de bureau | 7.0 Unités | 300.0 | TVA 10% | 2100.0 |
| Boîte de rangement | 41.0 Unités | 14.0 | TVA 20% | 574.0 |
| Tiroir noir | 82.0 Unités | 20.0 |  | 1640.0 |

    ## Source Locator

    - original_file: originals/FACTU2015020048.jpg
    - field_source: dataset XML/PDF text extract
    - quote: Invoice number FA02/2015/020059; Supplier Marc Demo; Total amount 82,003.30 EUR; PO reference BC06263.
