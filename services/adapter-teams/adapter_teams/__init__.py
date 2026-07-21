"""WS-A adapter Microsoft Teams — PROVISÃO (Fase 4, escopo órfão).

Espelha adapter-slack/adapter-github/adapter-jira: inbound (mensagem/menção
Teams -> ConversationEvent pelas 4 defesas de intake) e outbound (mensagem de
status única, editada in-place, via `MutableCommentWriter` com backend Teams).

NÃO ATIVADO: a decisão de ligar Teams é de negócio/roadmap (Fase 4+). O único
bloqueio de código para ativação é a fundação (`Platform`/CHECKs de plataforma
em packages/contracts + migrações 0001), que este workstream não edita nesta
sessão. Ver README.md e activation.sql."""
