# database.py
import os
import uuid 
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, Index, inspect
from sqlalchemy.engine import Engine
from sqlite3 import Connection as SQLite3Connection
from sqlalchemy.orm import relationship
import logging

db = SQLAlchemy()

@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    if isinstance(dbapi_connection, SQLite3Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

def generate_uuid():
    return str(uuid.uuid4())

class Cliente(db.Model):
    __tablename__ = 'cliente'
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(36), unique=True, nullable=False) 
    nome = db.Column(db.String(150), nullable=False)
    email_principal = db.Column(db.String(120), unique=True, nullable=True)
    telefone_principal = db.Column(db.String(20), nullable=True)

    contatos = db.relationship('Contato', backref='cliente', lazy=True, cascade="all, delete-orphan")
    boletos = db.relationship('Boleto', backref='cliente', lazy=True, cascade="all, delete-orphan")

    __table_args__ = (Index('ix_cliente_public_id', 'public_id'),)

    def __repr__(self):
        return f'<Cliente {self.nome} ({self.public_id})>'

class Contato(db.Model):
    __tablename__ = 'contato'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(120), nullable=True)
    telefone = db.Column(db.String(20), nullable=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('cliente.id', name='fk_contato_cliente_id'), nullable=False)
    is_principal = db.Column(db.Boolean, default=False, nullable=False)

    def __repr__(self):
        return f'<Contato {self.nome} (ClienteID: {self.cliente_id}, Principal: {self.is_principal})>'

class Boleto(db.Model):
    __tablename__ = 'boleto'
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(36), unique=True, nullable=False, default=generate_uuid)
    descricao_base = db.Column(db.String(180), nullable=False, default="Cobrança")
    descricao_completa = db.Column(db.String(200), nullable=True)
    valor = db.Column(db.Float, nullable=False)
    data_vencimento = db.Column(db.Date, nullable=False)
    periodicidade_replicacao = db.Column(db.String(20), nullable=False, default='mensal')
    numero_parcelas = db.Column(db.Integer, nullable=False, default=1)
    grupo_replicacao_id = db.Column(db.String(36), nullable=True)
    parcela_atual = db.Column(db.Integer, default=1, nullable=False)
    cliente_id = db.Column(db.Integer, db.ForeignKey('cliente.id', name='fk_boleto_cliente_id'), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pendente')
    data_pagamento = db.Column(db.Date, nullable=True)

    __table_args__ = (Index('ix_boleto_public_id', 'public_id'),)

    def __repr__(self):
        return f'<Boleto {self.id} ({self.public_id}) - Status: {self.status}>'

class ConfiguracaoEmail(db.Model):
    __tablename__ = 'configuracao_email'
    id = db.Column(db.Integer, primary_key=True)
    
    email_remetente = db.Column(db.String(120), nullable=True) # Pode ser nulo se não configurado
    senha_remetente = db.Column(db.String(120), nullable=True) # Pode ser nulo se não configurado
    nome_remetente = db.Column(db.String(150), nullable=True, default="Sistema de Cobranças")

    # Campos fixos para Gmail (usados pela lógica do app.py, não mais editáveis na UI simplificada)
    mail_server = db.Column(db.String(120), default='smtp.gmail.com')
    mail_port = db.Column(db.Integer, default=587)
    mail_use_tls = db.Column(db.Boolean, default=True)
    mail_use_ssl = db.Column(db.Boolean, default=False)

    # MODIFICADO: Texto padrão para usar {nome_do_contato_principal}
    texto_padrao_email = db.Column(db.Text, nullable=True, 
                                   default="<p>Olá {nome_do_contato_principal},</p><p>Segue o boleto referente a <strong>{descricao_boleto}</strong> no valor de <strong>R$ {valor_boleto}</strong>, com vencimento em <em>{data_vencimento}</em>.</p><p>Qualquer dúvida, estamos à disposição.</p><p>Atenciosamente,<br>{nome_remetente_empresa}</p>")
    dia_semana_envio = db.Column(db.String(20), nullable=True, default='segunda') 
    horario_envio = db.Column(db.Time, nullable=True) 
    dias_antecedencia_vencimento = db.Column(db.Integer, nullable=True, default=3)

    def __repr__(self):
        return f'<ConfiguracaoEmail {self.email_remetente}>'

class EmailLog(db.Model):
    __tablename__ = 'email_logs'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    horario_disparo = db.Column(db.TEXT, nullable=False)
    email_remetente = db.Column(db.String(120), nullable=True)
    email_destinatario = db.Column(db.TEXT, nullable=False)
    email_cc = db.Column(db.String(500), nullable=True) # Novo campo para e-mails em CC
    nome_empresa = db.Column(db.String(150), nullable=True)  # Pode ser nulo se não houver associação direta com empresa
    nome_contato = db.Column(db.TEXT, nullable=True)
    # Referência ao boleto e cliente usando ForeignKey, mais robusto
    boleto_id = db.Column(db.Integer, db.ForeignKey('boleto.id', name='fk_email_log_boleto_id'), nullable=True) # Permitir nulo caso o boleto seja excluído
    cliente_id = db.Column(db.Integer, db.ForeignKey('cliente.id', name='fk_email_log_cliente_id'), nullable=True) # Permitir nulo caso o cliente seja excluído
    data_boleto = db.Column(db.TEXT, nullable=False)
    assunto = db.Column(db.String(255), nullable=True)
    mensagem_corpo = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), nullable=True) # Ex: sucesso, falha_autenticacao, etc.
    detalhes = db.Column(db.Text, nullable=True) # Detalhes adicionais sobre o status

    boleto = relationship('Boleto', backref='logs_email', lazy=True)
    cliente = relationship('Cliente', backref='logs_email', lazy=True)
def init_app(app):
    base_dir = os.path.abspath(os.path.dirname(__file__))
    database_path = os.path.join(base_dir, 'cobrancas.db')
    # Garante que a URI do banco de dados seja configurada no app Flask
    if 'SQLALCHEMY_DATABASE_URI' not in app.config:
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + database_path
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    db.init_app(app)

    logger = app.logger if hasattr(app, 'logger') else logging.getLogger(__name__)
    
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        if app.debug:
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)

    logger.info(f"Banco de dados configurado em: {database_path}")

    with app.app_context():
        db.create_all()
