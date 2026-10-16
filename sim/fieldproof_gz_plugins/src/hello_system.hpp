// The smallest Fieldproof system plugin (task P0-T07): proves that a plugin from this package
// builds against gz-sim, loads and configures. Real systems (event hooks, metrics publisher,
// hydrodynamics) replace it from Phase 1 on.
#pragma once

#include <gz/sim/EntityComponentManager.hh>
#include <gz/sim/EventManager.hh>
#include <gz/sim/System.hh>
#include <sdf/Element.hh>

#include <memory>

namespace fieldproof {

class HelloSystem : public gz::sim::System, public gz::sim::ISystemConfigure {
 public:
  void Configure(const gz::sim::Entity& entity, const std::shared_ptr<const sdf::Element>& sdf,
                 gz::sim::EntityComponentManager& ecm, gz::sim::EventManager& event_mgr) override;
};

}  // namespace fieldproof
