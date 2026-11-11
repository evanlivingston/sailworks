#include <QtQml/QQmlExtensionPlugin>
#include <QtQml/qqml.h>

#include "routingretaineditem.h"

class SailworksRoutingPlugin : public QQmlExtensionPlugin {
  Q_OBJECT
  Q_PLUGIN_METADATA(IID QQmlExtensionInterface_iid)

 public:
  void registerTypes(const char *uri) override {
    qmlRegisterType<RoutingRetainedItem>(uri, 1, 0, "RoutingRetainedItem");
  }
};

#include "qmlplugin.moc"
