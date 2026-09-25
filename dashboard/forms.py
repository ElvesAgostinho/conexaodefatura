import json

from django import forms

from agt.models import AgtConfiguration
from sources.assistant import HEADER_LABELS, LINE_LABELS
from sources.mapping import CURSOR_TYPES
from sources.models import DataSource


class PrettyJSONField(forms.JSONField):
    def prepare_value(self, value):
        if isinstance(value, str):
            return value
        return json.dumps(value or {}, indent=2, ensure_ascii=False)


class _SourceBaseForm(forms.ModelForm):
    kind = None

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        self.instance.company = company
        self.instance.kind = self.kind
        if self.instance.pk and self.instance.invoices.exists():
            # O código identifica os documentos já importados: não pode mudar.
            self.fields["code"].disabled = True
            self.fields["code"].help_text = "Não pode ser alterado: já há documentos desta ligação."

    def clean_code(self):
        code = self.cleaned_data["code"]
        clash = DataSource.objects.filter(company=self.company, code=code).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("Já existe uma ligação com este código nesta empresa.")
        return code


class ApiSourceForm(_SourceBaseForm):
    kind = DataSource.Kind.API

    class Meta:
        model = DataSource
        fields = ["code", "name", "system", "active"]


class DatabaseSourceForm(_SourceBaseForm):
    kind = DataSource.Kind.DATABASE

    db_password = forms.CharField(
        label="Senha", required=False, strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="Guardada cifrada. Deixe vazio para manter a senha já guardada.",
    )
    clear_password = forms.BooleanField(label="Apagar a senha guardada", required=False)
    db_url = forms.CharField(
        label="URL de ligação", required=False, strip=True,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text="Só para 'Outro SGBD'. Ex.: firebird+fdb://utilizador:senha@servidor/caminho.fdb. Guardado cifrado.",
    )

    class Meta:
        model = DataSource
        fields = ["code", "name", "system", "active", "connection_mode", "db_engine", "db_host", "db_port",
                  "db_name", "db_user", "db_odbc_driver", "db_trust_server_certificate", "db_options", "connection"]
        widgets = {"connection_mode": forms.RadioSelect}

    def __init__(self, *args, keep_secrets=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["db_engine"].choices = [("", "— escolher —")] + list(DataSource.Engine.choices)
        if not self.instance.pk and not self.is_bound:
            self.initial.setdefault("db_engine", DataSource.Engine.MSSQL)
        if keep_secrets:
            # Depois de "Testar ligação" sem guardar, mantém a senha escrita no formulário.
            self.fields["db_password"].widget.render_value = True
            self.fields["db_url"].widget.render_value = True
        if self.instance.has_db_password:
            self.fields["db_password"].widget.attrs["placeholder"] = "•••••••• (guardada)"
        if self.instance.db_url_encrypted:
            self.fields["db_url"].widget.attrs["placeholder"] = "•••••••• (guardado)"

    def clean(self):
        data = super().clean()
        # Aplicado antes da validação do modelo, que verifica se o URL existe.
        if data.get("clear_password"):
            self.instance.db_password_encrypted = ""
        elif data.get("db_password"):
            self.instance.set_db_password(data["db_password"])
        if data.get("db_url"):
            self.instance.set_db_url(data["db_url"])
        if data.get("db_engine") != DataSource.Engine.URL:
            self.instance.db_url_encrypted = ""
        return data


class MappingJSONForm(forms.ModelForm):
    mapping = PrettyJSONField(
        label="Mapeamento (JSON)", required=False,
        widget=forms.Textarea(attrs={"rows": 22, "class": "mono", "spellcheck": "false"}),
    )

    class Meta:
        model = DataSource
        fields = ["mapping"]

    def clean_mapping(self):
        return self.cleaned_data.get("mapping") or {}


class TablesForm(forms.Form):
    documents_table = forms.ChoiceField(label="Tabela ou vista dos documentos (cabeçalhos)")
    lines_table = forms.ChoiceField(label="Tabela ou vista das linhas dos documentos")

    def __init__(self, *args, tables, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "— escolher —")] + [(t.qualified, f"{t.qualified} ({t.kind})") for t in tables]
        self.fields["documents_table"].choices = choices
        self.fields["lines_table"].choices = choices


class MappingAssistantForm(forms.Form):
    """Correspondência campo canónico -> coluna real (listas com as colunas da tabela)."""

    cursor_column = forms.ChoiceField(
        label="Coluna que cresce a cada documento novo",
        help_text="Ex.: o ID sequencial ou a data/hora de criação. Usada para ler só documentos novos.")
    cursor_type = forms.ChoiceField(label="Tipo dessa coluna", choices=[(c, c) for c in CURSOR_TYPES])
    link_column = forms.ChoiceField(
        label="Coluna das linhas que indica o documento",
        help_text="Tem o mesmo valor que o 'ID do documento na origem'.")
    batch_size = forms.IntegerField(label="Documentos por sincronização", min_value=1, max_value=1000, initial=200)
    start_date = forms.DateField(
        label="Começar a partir da data", required=False, widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Só importa documentos com esta data ou mais recentes (evita importar anos de histórico).")
    start_after = forms.CharField(
        label="ou começar depois do ID interno", required=False, max_length=100,
        help_text="Valor da coluna crescente a partir do qual começar (ex.: o ID da última fatura já tratada).")
    document_types = forms.MultipleChoiceField(
        label="Tipos de documento a importar", required=False, widget=forms.CheckboxSelectMultiple,
        help_text="Desmarque os que não são faturas (ex.: pró-formas, orçamentos). Todos marcados = sem filtro.")

    REQUIRED_HEADER = {"source_document_id", "document_type", "series", "document_number", "document_date",
                       "customer_name", "subtotal", "tax_amount", "total"}
    REQUIRED_LINE = {"description", "quantity", "unit_price", "tax_rate", "tax_amount", "total"}

    def __init__(self, *args, document_columns, line_columns, type_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.type_values = [value for value, _ in (type_choices or [])]
        self.fields["document_types"].choices = [(v, f"{v} ({n})") for v, n in (type_choices or [])]
        doc_choices = [("", "— não existe —")] + [(c, c) for c in document_columns]
        line_choices = [("", "— não existe —")] + [(c, c) for c in line_columns]
        self.fields["cursor_column"].choices = doc_choices[1:]
        self.fields["link_column"].choices = line_choices[1:]
        for name, label in HEADER_LABELS.items():
            self.fields[f"h_{name}"] = forms.ChoiceField(label=label, choices=doc_choices, required=False)
            if name not in ("source_document_id", "document_hash", "hash_control"):  # vêm sempre da origem
                self.fields[f"hd_{name}"] = forms.CharField(label="ou valor fixo", required=False, max_length=100, widget=forms.TextInput(attrs={"placeholder": "ou valor fixo"}))
        for name, label in LINE_LABELS.items():
            self.fields[f"l_{name}"] = forms.ChoiceField(label=label, choices=line_choices, required=False)
            self.fields[f"ld_{name}"] = forms.CharField(label="ou valor fixo", required=False, max_length=100, widget=forms.TextInput(attrs={"placeholder": "ou valor fixo"}))

    def header_rows(self):
        return [(name, label, self[f"h_{name}"], self[f"hd_{name}"] if f"hd_{name}" in self.fields else None,
                 name in self.REQUIRED_HEADER) for name, label in HEADER_LABELS.items()]

    def line_rows(self):
        return [(name, label, self[f"l_{name}"], self[f"ld_{name}"], name in self.REQUIRED_LINE)
                for name, label in LINE_LABELS.items()]

    def clean(self):
        data = super().clean()
        start_after = (data.get("start_after") or "").strip()
        data["start_after"] = start_after
        if start_after and data.get("cursor_type") == "int" and not start_after.lstrip("-").isdigit():
            self.add_error("start_after", "A coluna crescente é numérica: indique um número.")
        if self.type_values and not data.get("document_types") and self.is_bound:
            self.add_error("document_types", "Escolha pelo menos um tipo de documento.")
        # Todos marcados = sem filtro (um tipo novo no sistema de faturação não fica excluído).
        if set(data.get("document_types") or []) >= set(self.type_values):
            data["document_types"] = []
        if not data.get("h_source_document_id"):
            self.add_error("h_source_document_id", "Obrigatório: tem de vir de uma coluna.")
        for name in self.REQUIRED_HEADER - {"source_document_id"}:
            if not data.get(f"h_{name}") and not data.get(f"hd_{name}"):
                self.add_error(f"h_{name}", "Escolha uma coluna ou indique um valor fixo.")
        for name in self.REQUIRED_LINE:
            if not data.get(f"l_{name}") and not data.get(f"ld_{name}"):
                self.add_error(f"l_{name}", "Escolha uma coluna ou indique um valor fixo.")
        return data

    def split(self):
        d = self.cleaned_data
        return (
            {n: d.get(f"h_{n}", "") for n in HEADER_LABELS},
            {n: d.get(f"hd_{n}", "") for n in HEADER_LABELS if f"hd_{n}" in d},
            {n: d.get(f"l_{n}", "") for n in LINE_LABELS},
            {n: d.get(f"ld_{n}", "") for n in LINE_LABELS},
        )

    @classmethod
    def initial_from_mapping(cls, mapping: dict) -> dict:
        filters = mapping.get("filters") or {}
        start_after = str(mapping.get("initial_cursor", "") or "")
        initial = {
            "cursor_column": mapping.get("cursor_column", ""),
            "cursor_type": mapping.get("cursor_type", "int"),
            "batch_size": mapping.get("batch_size", 200),
            "link_column": mapping.get("assistant", {}).get("link_column", ""),
            "start_date": filters.get("start_date") or None,
            "start_after": "" if start_after in ("0", "", "1900-01-01T00:00:00") else start_after,
        }
        if filters.get("document_types"):
            initial["document_types"] = list(filters["document_types"])
        for name, column in mapping.get("fields", {}).items():
            initial[f"h_{name}"] = column
        for name, value in mapping.get("defaults", {}).items():
            initial[f"hd_{name}"] = value
        for name, column in mapping.get("line_fields", {}).items():
            initial[f"l_{name}"] = column
        for name, value in mapping.get("line_defaults", {}).items():
            initial[f"ld_{name}"] = value
        return initial


class AgtConfigurationForm(forms.ModelForm):
    """Configuração AGT. Senha e chaves são só de escrita: nunca voltam a ser mostradas."""

    api_password = forms.CharField(
        label="Senha da API", required=False, strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="Guardada cifrada. Deixe vazio para manter a senha já guardada.")
    issuer_key_file = forms.FileField(
        label="Chave privada do contribuinte (ficheiro .pem, .key, .pfx ou .p12)", required=False,
        help_text="Gerada pela AGT e obtida pelo cliente no portal do contribuinte.")
    issuer_key_password = forms.CharField(label="Senha do ficheiro da chave (se tiver)", required=False, strip=False,
                                          widget=forms.PasswordInput(render_value=False))
    clear_issuer_key = forms.BooleanField(label="Apagar a chave do contribuinte guardada", required=False)
    producer_key_file = forms.FileField(
        label="Chave privada do produtor (opcional)", required=False,
        help_text="Só se o produtor do software de faturação a fornecer. Em alternativa, cole a assinatura acima.")
    producer_key_password = forms.CharField(label="Senha do ficheiro da chave do produtor (se tiver)",
                                            required=False, strip=False,
                                            widget=forms.PasswordInput(render_value=False))
    clear_producer_key = forms.BooleanField(label="Apagar a chave do produtor guardada", required=False)

    class Meta:
        model = AgtConfiguration
        fields = [
            "environment", "base_url", "api_username", "software_name", "software_version",
            "software_certificate_number", "signature_version", "software_signature", "auto_send",
            "timeout_seconds", "max_attempts", "retry_delay_seconds",
        ]
        widgets = {"software_signature": forms.Textarea(attrs={"rows": 3, "class": "mono", "spellcheck": "false"})}

    def clean(self):
        from agt.keys import KeyFileError, load_private_key

        data = super().clean()
        if data.get("api_password"):
            self.instance.set_api_password(data["api_password"])
        for kind in ("issuer", "producer"):
            upload = data.get(f"{kind}_key_file")
            if data.get(f"clear_{kind}_key"):
                self.instance.clear_key(kind)
            elif upload:
                try:
                    self.instance.set_key(kind, load_private_key(upload.read(), data.get(f"{kind}_key_password") or ""))
                except KeyFileError as exc:
                    self.add_error(f"{kind}_key_file", str(exc))
        signature = (data.get("software_signature") or "").strip()
        if signature and signature.count(".") != 2:
            self.add_error("software_signature", "Não parece uma assinatura JWS (três partes separadas por pontos).")
        data["software_signature"] = signature
        return data


class ApiKeyForm(forms.Form):
    name = forms.CharField(label="Nome da chave", max_length=100, help_text="Ex.: ERP da loja, POS do restaurante.")
    source = forms.ModelChoiceField(label="Ligação API", queryset=DataSource.objects.none(),
                                    help_text="Os documentos enviados com esta chave pertencem a esta ligação.")

    def __init__(self, *args, company, source=None, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = DataSource.objects.filter(company=company, kind=DataSource.Kind.API, active=True)
        self.fields["source"].queryset = queryset
        if source is not None:
            self.fields["source"].initial = source.pk
            self.fields["source"].widget = forms.HiddenInput()
