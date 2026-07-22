# Optional toast for cadence on Windows 10/11 (user session, no admin).
param([string]$Text = "Task Ledger: proposals awaiting review")
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText01)
  $xml.GetElementsByTagName("text").Item(0).AppendChild($xml.CreateTextNode($Text)) | Out-Null
  $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier(
    "Task Ledger").Show($toast)
} catch { Write-Output "toast unavailable: $_" }
