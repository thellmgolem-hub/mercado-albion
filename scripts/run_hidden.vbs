' Executa um .cmd sem abrir janela (usado pelo Agendador de Tarefas)
Set sh = CreateObject("WScript.Shell")
sh.Run """" & WScript.Arguments(0) & """", 0, False
