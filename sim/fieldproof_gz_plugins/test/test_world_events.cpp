// Tests of the WorldEvents system (task P2-T03): a headless server with physics and wind
// effects, no gravity (so a spawned box stays where it is put), the plugin loaded from this
// package's build directory (GZ_SIM_SYSTEM_PLUGIN_PATH, set by CMake; the test does not link the
// plugin library, since an executable carrying a plugin registration hides every other
// plugin from gz-plugin's loader), and the four services called through gz-transport as the
// scenario engine calls them.
#include "world_events.hpp"

#include <gz/math/Pose3.hh>
#include <gz/math/Vector3.hh>
#include <gz/msgs/Utility.hh>
#include <gz/msgs/empty.pb.h>
#include <gz/sim/Server.hh>
#include <gz/sim/ServerConfig.hh>
#include <gz/sim/TestFixture.hh>
#include <gz/sim/components/LinearVelocitySeed.hh>
#include <gz/sim/components/Model.hh>
#include <gz/sim/components/Name.hh>
#include <gz/sim/components/Pose.hh>
#include <gz/sim/components/Wind.hh>
#include <gz/transport/Node.hh>

#include <gtest/gtest.h>

#include <map>
#include <mutex>
#include <optional>
#include <string>

namespace {

constexpr unsigned int kTimeoutMs = 5000;
constexpr const char* kWorld = "events_test";

const char* kWorldSdf = R"sdf(<?xml version="1.0"?>
<sdf version="1.11">
  <world name="events_test">
    <physics name="1ms" type="ignored">
      <max_step_size>0.001</max_step_size>
      <real_time_factor>0</real_time_factor>
    </physics>
    <gravity>0 0 0</gravity>
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
    <plugin filename="gz-sim-wind-effects-system" name="gz::sim::systems::WindEffects"/>
    <plugin filename="fieldproof_world_events" name="fieldproof::WorldEvents"/>
  </world>
</sdf>)sdf";

const char* kBoxSdf =
    "<?xml version='1.0'?><sdf version='1.11'><model name='box'><link name='link'>"
    "<inertial><mass>1</mass></inertial>"
    "<collision name='c'><geometry><box><size>0.5 0.5 0.5</size></box></geometry></collision>"
    "<visual name='v'><geometry><box><size>0.5 0.5 0.5</size></box></geometry></visual>"
    "</link></model></sdf>";

/// What the fixture observed after each step: model poses by name, the wind seed.
struct Observed {
  std::mutex mutex;
  std::map<std::string, gz::math::Pose3d> poses;
  std::optional<gz::math::Vector3d> wind_seed;

  std::optional<gz::math::Pose3d> PoseOf(const std::string& name) {
    const std::lock_guard<std::mutex> lock(mutex);
    const auto it = poses.find(name);
    if (it == poses.end()) {
      return std::nullopt;
    }
    return it->second;
  }
};

class WorldEventsTest : public ::testing::Test {
 protected:
  void SetUp() override {
    gz::sim::ServerConfig config;
    config.SetSdfString(kWorldSdf);
    fixture_ = std::make_unique<gz::sim::TestFixture>(config);
    fixture_
        ->OnPostUpdate([this](const gz::sim::UpdateInfo&,
                              const gz::sim::EntityComponentManager& ecm) {
          const std::lock_guard<std::mutex> lock(observed_.mutex);
          observed_.poses.clear();
          ecm.Each<gz::sim::components::Model, gz::sim::components::Name,
                   gz::sim::components::Pose>(
              [this](const gz::sim::Entity&, const gz::sim::components::Model*,
                     const gz::sim::components::Name* name, const gz::sim::components::Pose* pose) {
                observed_.poses[name->Data()] = pose->Data();
                return true;
              });
          const gz::sim::Entity wind = ecm.EntityByComponents(gz::sim::components::Wind());
          const auto* seed = ecm.Component<gz::sim::components::WorldLinearVelocitySeed>(wind);
          observed_.wind_seed =
              seed == nullptr ? std::nullopt : std::optional<gz::math::Vector3d>(seed->Data());
        })
        .Finalize();
    // Run the server in its own thread, unpaused, so service requests are applied by the steps.
    ASSERT_TRUE(fixture_->Server()->Run(false, 0, false));
  }

  void TearDown() override {
    fixture_->Server()->Stop();
    fixture_.reset();
  }

  template <typename Request>
  std::string Call(const std::string& verb, const Request& request) {
    gz::msgs::StringMsg reply;
    bool result = false;
    const bool executed =
        node_.Request(fieldproof::ServiceName(kWorld, verb), request, kTimeoutMs, reply, result);
    EXPECT_TRUE(executed) << verb << ": the service did not answer";
    EXPECT_TRUE(result) << verb << ": the service failed";
    return reply.data();
  }

  std::string Spawn(const std::string& name, const gz::math::Pose3d& pose,
                    bool allow_renaming = false) {
    gz::msgs::EntityFactory request;
    request.set_sdf(kBoxSdf);
    request.set_name(name);
    request.set_allow_renaming(allow_renaming);
    gz::msgs::Set(request.mutable_pose(), pose);
    return Call("spawn", request);
  }

  /// Wait until `predicate` holds in the observed state (a few steps), up to ~2 s.
  template <typename Predicate>
  bool Eventually(Predicate predicate) {
    for (int i = 0; i < 200; ++i) {
      if (predicate()) {
        return true;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return predicate();
  }

  std::unique_ptr<gz::sim::TestFixture> fixture_;
  gz::transport::Node node_;
  Observed observed_;
};

TEST_F(WorldEventsTest, ServiceNamesLiveUnderTheWorld) {
  EXPECT_EQ(fieldproof::ServiceName("depot", "spawn"), "/world/depot/fieldproof/spawn");
}

TEST_F(WorldEventsTest, SpawnCreatesTheModelAtThePose) {
  const gz::math::Pose3d pose(1.0, 2.0, 0.25, 0, 0, 1.5);
  const std::string report = Spawn("crate", pose);
  EXPECT_EQ(report, "spawned model crate at (1, 2, 0.25, yaw 1.5)");
  EXPECT_TRUE(fixture_->Server()->HasEntity("crate"));
  ASSERT_TRUE(Eventually([&] { return observed_.PoseOf("crate").has_value(); }));
  const auto observed = *observed_.PoseOf("crate");
  EXPECT_NEAR(observed.Pos().X(), 1.0, 1e-3);
  EXPECT_NEAR(observed.Pos().Y(), 2.0, 1e-3);
  EXPECT_NEAR(observed.Pos().Z(), 0.25, 1e-3);
  EXPECT_NEAR(observed.Rot().Yaw(), 1.5, 1e-3);
}

TEST_F(WorldEventsTest, SpawnRefusesATakenNameUnlessRenamingIsAllowed) {
  const gz::math::Pose3d pose(0, 0, 0.25, 0, 0, 0);
  EXPECT_EQ(Spawn("crate", pose).rfind("spawned", 0), 0u);
  EXPECT_EQ(Spawn("crate", pose),
            "error: spawn: a model called crate already exists in world events_test");
  EXPECT_EQ(Spawn("crate", pose, true), "spawned model crate_0 at (0, 0, 0.25, yaw 0)");
  EXPECT_TRUE(fixture_->Server()->HasEntity("crate_0"));
}

TEST_F(WorldEventsTest, SpawnReportsBadRequests) {
  gz::msgs::EntityFactory request;
  request.set_sdf("<sdf version='1.11'><model name='x'><link name='l'><collision name='c'>");
  EXPECT_EQ(Call("spawn", request).rfind("error: spawn: the SDF does not parse", 0), 0u);
  request.set_sdf(
      "<?xml version='1.0'?><sdf version='1.11'><light name='sun' type='directional'/></sdf>");
  EXPECT_EQ(Call("spawn", request), "error: spawn: the SDF holds no model");
  gz::msgs::EntityFactory empty;
  EXPECT_EQ(Call("spawn", empty), "error: spawn: the request carries neither sdf nor sdf_filename");
}

TEST_F(WorldEventsTest, TeleportMovesAModel) {
  Spawn("crate", gz::math::Pose3d(1.0, 2.0, 0.25, 0, 0, 0));
  gz::msgs::Pose request;
  request.set_name("crate");
  gz::msgs::Set(&request, gz::math::Pose3d(5.0, -6.0, 0.25, 0, 0, -1.0));
  EXPECT_EQ(Call("teleport", request), "teleported model crate to (5, -6, 0.25, yaw -1)");
  ASSERT_TRUE(Eventually([&] {
    const auto pose = observed_.PoseOf("crate");
    return pose && pose->Pos().Distance(gz::math::Vector3d(5.0, -6.0, 0.25)) < 1e-2;
  }));
  EXPECT_NEAR(observed_.PoseOf("crate")->Rot().Yaw(), -1.0, 1e-2);
  // A pose without an orientation means no rotation.
  gz::msgs::Pose upright;
  upright.set_name("crate");
  upright.mutable_position()->set_x(0.5);
  upright.mutable_position()->set_z(0.25);
  EXPECT_EQ(Call("teleport", upright), "teleported model crate to (0.5, 0, 0.25, yaw 0)");
  ASSERT_TRUE(Eventually([&] {
    const auto pose = observed_.PoseOf("crate");
    return pose && std::abs(pose->Rot().Yaw()) < 1e-2 && std::abs(pose->Pos().X() - 0.5) < 1e-2;
  }));
  gz::msgs::Pose unknown;
  unknown.set_name("ghost");
  EXPECT_EQ(Call("teleport", unknown),
            "error: teleport: no model called ghost in world events_test");
  EXPECT_EQ(Call("teleport", gz::msgs::Pose()), "error: teleport: the request names no model");
}

TEST_F(WorldEventsTest, RemoveDeletesAModel) {
  Spawn("crate", gz::math::Pose3d(1.0, 2.0, 0.25, 0, 0, 0));
  ASSERT_TRUE(Eventually([&] { return observed_.PoseOf("crate").has_value(); }));
  gz::msgs::Entity request;
  request.set_name("crate");
  EXPECT_EQ(Call("remove", request), "removed model crate");
  ASSERT_TRUE(Eventually([&] { return !observed_.PoseOf("crate").has_value(); }));
  EXPECT_FALSE(fixture_->Server()->HasEntity("crate"));
  EXPECT_EQ(Call("remove", request), "error: remove: no model called crate in world events_test");
  EXPECT_EQ(Call("remove", gz::msgs::Entity()), "error: remove: the request names no model");
}

TEST_F(WorldEventsTest, SetWindChangesTheWorldWindAndWindEffects) {
  gz::msgs::Wind request;
  request.set_enable_wind(true);
  gz::msgs::Set(request.mutable_linear_velocity(), gz::math::Vector3d(3.0, -1.0, 0.0));
  EXPECT_EQ(Call("set_wind", request), "wind set to (3, -1, 0) m/s");
  ASSERT_TRUE(Eventually([&] {
    const std::lock_guard<std::mutex> lock(observed_.mutex);
    return observed_.wind_seed &&
           observed_.wind_seed->Distance(gz::math::Vector3d(3, -1, 0)) < 1e-6;
  }));
  // WindEffects took the message: its wind_info service reports the same wind.
  gz::msgs::Wind info;
  bool result = false;
  ASSERT_TRUE(Eventually([&] {
    return node_.Request("/world/events_test/wind_info", gz::msgs::Empty(), kTimeoutMs, info,
                         result) &&
           result && info.enable_wind() && std::abs(info.linear_velocity().x() - 3.0) < 1e-6;
  }));
  EXPECT_NEAR(info.linear_velocity().y(), -1.0, 1e-6);
  gz::msgs::Wind off;
  off.set_enable_wind(false);
  gz::msgs::Set(off.mutable_linear_velocity(), gz::math::Vector3d(9.0, 9.0, 9.0));
  EXPECT_EQ(Call("set_wind", off), "wind disabled");
  ASSERT_TRUE(Eventually([&] {
    const std::lock_guard<std::mutex> lock(observed_.mutex);
    return observed_.wind_seed && observed_.wind_seed->Length() < 1e-6;
  }));
}

}  // namespace
