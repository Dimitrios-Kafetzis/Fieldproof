#include "hello_system.hpp"

#include "fieldproof_gz_plugins/version.hpp"

#include <gz/common/Console.hh>
#include <gz/plugin/Register.hh>

namespace fieldproof {

void HelloSystem::Configure(const gz::sim::Entity& entity,
                            const std::shared_ptr<const sdf::Element>& /*sdf*/,
                            gz::sim::EntityComponentManager& /*ecm*/,
                            gz::sim::EventManager& /*event_mgr*/) {
  gzmsg << "[" << fieldproof_gz_plugins::Banner() << "] HelloSystem configured on entity ["
        << entity << "]\n";
}

}  // namespace fieldproof

GZ_ADD_PLUGIN(fieldproof::HelloSystem, gz::sim::System, fieldproof::HelloSystem::ISystemConfigure)
GZ_ADD_PLUGIN_ALIAS(fieldproof::HelloSystem, "fieldproof::HelloSystem")
